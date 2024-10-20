import inspect
from dataclasses import dataclass
from threading import Thread
from typing import Any, AsyncIterator, Dict, Iterator, List, Literal, Optional, Union

import json
import torch
from tqdm import tqdm
from transformers import GenerationConfig, LogitsProcessorList, StoppingCriteriaList
from transformers.utils import is_torch_npu_available

from swift.llm import Template, to_device
from swift.plugin import Metric
from swift.tuners import Swift
from swift.utils import get_logger
from .infer_engine import InferEngine
from .protocol import (ChatCompletionResponse, ChatCompletionResponseChoice, ChatCompletionResponseStreamChoice,
                       ChatCompletionStreamResponse, ChatMessage, DeltaMessage, InferRequest, RequestConfig,
                       random_uuid)
from .utils import InferStreamer, InferTools, LogitsStreamer, StopWordsCriteria, TokensIteratorStreamer

logger = get_logger()


@dataclass
class PtLoRARequest:
    lora_name: str
    lora_int_id: int  # not use, only compat with vllm
    lora_local_path: str


class _GenerationConfig(GenerationConfig):

    def __repr__(self) -> str:
        parameters = inspect.signature(self.to_json_string).parameters
        kwargs = {}
        if 'ignore_metadata' in parameters:
            kwargs['ignore_metadata'] = True
        gen_kwargs = json.loads(self.to_json_string(**kwargs))
        gen_kwargs.pop('transformers_version', None)
        return f'GenerationConfig({gen_kwargs})'


class PtEngine(InferEngine):

    def __init__(
            self,
            model_id_or_path: str,
            torch_dtype: Optional[torch.dtype] = None,
            *,
            model_type: Optional[str] = None,
            use_hf: Optional[bool] = None,
            revision: Optional[str] = None,
            attn_impl: Literal['flash_attn', 'sdpa', 'eager', 'auto'] = 'auto',
            # model kwargs
            device_map: Optional[Union[str, Dict[str, Any]]] = None,
            quantization_config: Optional[Dict[str, Any]] = None,
            model_kwargs: Optional[Dict[str, Any]] = None):
        if device_map is not None:
            model_kwargs['device_map'] = device_map
        if quantization_config is not None:
            model_kwargs['quantization_config'] = quantization_config
        self._prepare_model_tokenizer(
            model_id_or_path,
            torch_dtype,
            True,
            model_type=model_type,
            use_hf=use_hf,
            revision=revision,
            attn_impl=attn_impl,
            model_kwargs=model_kwargs)
        self.engine = self.model
        self.generation_config = self.model.generation_config
        self._lora_request_pool = {}

    def _prepare_generation_config(self, request_config: RequestConfig) -> GenerationConfig:
        kwargs = {'max_new_tokens': request_config.max_tokens}
        # not use: 'n', 'best_of', 'frequency_penalty', 'presence_penalty'
        for key in ['length_penalty']:
            kwargs[key] = getattr(request_config, key)
        for key in ['temperature', 'top_k', 'top_p', 'repetition_penalty', 'num_beams']:
            new_value = getattr(request_config, key)
            if new_value is None:
                kwargs[key] = getattr(self.generation_config, key)
            else:
                kwargs[key] = new_value

        if not self.generation_config.do_sample:
            kwargs['temperature'] = 0
        if kwargs['temperature'] == 0:
            kwargs['do_sample'] = False
            kwargs['temperature'] = 1
            kwargs['top_p'] = 1
            kwargs['top_k'] = 50
        else:
            kwargs['do_sample'] = True
        kwargs['return_dict_in_generate'] = True
        if request_config.logprobs:
            kwargs['output_logits'] = True
        generation_config = _GenerationConfig(**kwargs)
        generation_config.top_logprobs = request_config.top_logprobs
        return self._set_generation_config_default_value(generation_config)

    def _set_generation_config_default_value(self, generation_config: GenerationConfig) -> GenerationConfig:
        for k, v in self.generation_config.to_dict().items():
            new_v = getattr(generation_config, k, None)
            if k in ['max_length']:
                continue
            if k in ['no_repeat_ngram_size'] or v is not None and new_v is None:
                setattr(generation_config, k, v)
        return generation_config

    def _add_stop_words(self, generation_config: GenerationConfig, request_config: RequestConfig,
                        template: Template) -> None:
        stop_words = (request_config.stop or []) + template.stop_words
        stop_words += [template.suffix[-1], self.tokenizer.eos_token]
        generation_config.stop_words = self._get_stop_words(stop_words)

    @staticmethod
    def preprocess_logits(batched_logits: Optional[List[torch.Tensor]], batched_generate_ids: torch.Tensor,
                          top_logprobs: int):
        batch_size = batched_generate_ids.shape[0]
        if batched_logits is None:
            return None
        batched_logprobs = []
        for i in range(batch_size):
            logprobs_list = []
            generate_ids = batched_generate_ids[i]
            for j, logits in enumerate(batched_logits):
                token = generate_ids[j].item()
                logprobs = torch.log_softmax(logits[i], -1)
                tokens = [token] + logprobs.argsort(descending=True, dim=-1)[:top_logprobs].tolist()
                logprobs_list.append({token: logprobs[token].item() for token in tokens})
            batched_logprobs.append(logprobs_list)
        return batched_logprobs

    async def _infer_stream_async(
            self,
            template: Template,
            infer_request: InferRequest,
            request_config: RequestConfig,
            *,
            lora_request: Optional[PtLoRARequest] = None) -> AsyncIterator[ChatCompletionStreamResponse]:
        gen = self.infer(template, [infer_request], request_config, use_tqdm=False, lora_request=lora_request)
        for response in gen:
            yield response[0]

    async def _infer_full_async(self,
                                template: Template,
                                infer_request: InferRequest,
                                request_config: RequestConfig,
                                *,
                                lora_request: Optional[PtLoRARequest] = None) -> ChatCompletionResponse:
        return self.infer(template, [infer_request], request_config, use_tqdm=False, lora_request=lora_request)[0]

    @staticmethod
    def _get_finish_reason(generation_config: GenerationConfig, num_prompt_tokens: int, is_finished: bool):
        if is_finished:
            if num_prompt_tokens >= generation_config.max_new_tokens:
                finish_reason = 'length'
            else:
                finish_reason = 'stop'
        else:
            finish_reason = None
        return finish_reason

    @staticmethod
    def _update_batched_logprobs(batched_logprobs: List[torch.Tensor], logits_streamer: Optional[LogitsStreamer],
                                 generate_ids: torch.Tensor, top_logprobs: int) -> None:
        seq_len = generate_ids.shape[1] - len(batched_logprobs[0])
        if logits_streamer is None or seq_len == 0:
            return batched_logprobs

        res = []
        for i in range(seq_len):
            res.append(logits_streamer.queue.get())
        new_batched_logprobs = PtEngine.preprocess_logits(res, generate_ids[:, -seq_len:], top_logprobs)
        for logprobs, new_logprobs in zip(batched_logprobs, new_batched_logprobs):
            logprobs += new_logprobs

    def _infer_stream(
            self,
            template: Template,
            inputs: Dict[str, Any],
            generation_config: GenerationConfig,
            *,
            lora_request: Optional[PtLoRARequest] = None) -> Iterator[List[Optional[ChatCompletionStreamResponse]]]:
        kwargs = {}
        if lora_request is not None:
            kwargs['adapter_names'] = self._get_adapter_names(lora_request)
        num_prompt_tokens = self._get_num_tokens(inputs)
        stopping_criteria = StoppingCriteriaList([StopWordsCriteria(self.tokenizer, generation_config.stop_words)])
        if generation_config.num_beams != 1:
            error_msg = 'Streaming generation does not support beam search.'
            raise ValueError(error_msg)

        streamer = TokensIteratorStreamer()

        def _model_generate(*args, **kwargs):
            if is_torch_npu_available():
                torch.npu.set_device(self.model.device)
            self.model.generate(*args, **kwargs)

        logits_streamer = None
        if generation_config.output_logits:
            logits_streamer = LogitsStreamer()
            kwargs['logits_processor'] = LogitsProcessorList([logits_streamer])

        thread = Thread(
            target=_model_generate,
            kwargs={
                'generation_config': generation_config,
                'stopping_criteria': stopping_criteria,
                'streamer': streamer,
                **inputs,
                **kwargs
            })
        thread.start()
        batch_size = inputs['attention_mask'].shape[0]
        all_is_finished = False
        is_finished = [False] * batch_size
        infer_streamers = [InferStreamer(template) for _ in range(batch_size)]
        request_id_list = [f'chatcmpl-{random_uuid()}' for _ in range(batch_size)]
        token_idxs = [0] * batch_size

        raw_batched_generate_ids = None  # or torch.Tensor: [batch_size, seq_len]
        batched_logprobs = [[] for _ in range(batch_size)]
        while not all_is_finished:
            try:
                batched_tokens = next(streamer)
                if batched_tokens.ndim == 1:
                    batched_tokens = batched_tokens[:, None]

                raw_batched_generate_ids = torch.concat(
                    [batched_tokens]
                    if raw_batched_generate_ids is None else [raw_batched_generate_ids, batched_tokens],
                    dim=1)
            except StopIteration:
                all_is_finished = True

            batched_generate_ids = template.get_generate_ids(raw_batched_generate_ids, num_prompt_tokens)
            self._update_batched_logprobs(batched_logprobs, logits_streamer, batched_generate_ids,
                                          generation_config.top_logprobs or 1)
            # TODO: MLLM
            res = []
            for i in range(batched_generate_ids.shape[0]):
                if is_finished[i]:
                    res.append(None)
                    continue
                generate_ids = batched_generate_ids[i]

                # ignore pad_token
                masks = generate_ids != generation_config.pad_token_id
                generate_ids = generate_ids[masks].tolist()
                logprobs_list = None
                if batched_logprobs[i]:
                    logprobs_list = [logprobs for m, logprobs in zip(masks, batched_logprobs[i]) if m.item()]

                is_finished[i] = (
                    all_is_finished or is_finished[i]
                    or len(generate_ids) > 0 and generate_ids[-1] == generation_config.pad_token_id)
                delta_text = infer_streamers[i].get_printable_text(generate_ids, is_finished[i])
                if not delta_text and not is_finished[i]:
                    res.append(None)
                    continue
                logprobs = self._get_logprobs(self.tokenizer, logprobs_list, generate_ids[token_idxs[i]:],
                                              generation_config.top_logprobs)
                token_idxs[i] = len(generate_ids)

                usage_info = self._get_usage_info(num_prompt_tokens, len(generate_ids))
                toolcall = self._get_toolcall(generate_ids, is_finished[i])
                finish_reason = self._get_finish_reason(generation_config, num_prompt_tokens, is_finished[i])

                choices = [
                    ChatCompletionResponseStreamChoice(
                        index=0,
                        delta=DeltaMessage(role='assistant', content=delta_text, tool_calls=toolcall),
                        finish_reason=finish_reason,
                        logprobs=logprobs)
                ]
                res.append(
                    ChatCompletionStreamResponse(
                        model=self.model_dir, choices=choices, usage=usage_info, id=request_id_list[i]))
            if any(res):
                yield res

    def _get_adapter_names(self, lora_request: PtLoRARequest) -> List[str]:
        if lora_request.lora_name in self._lora_request_pool:
            assert lora_request == self._lora_request_pool[lora_request.lora_name]
        else:
            self._lora_request_pool[lora_request.lora_name] = lora_request
            Swift.from_pretrained(self.model, lora_request.lora_local_path, lora_request.lora_name, inference_mode=True)
        return [lora_request.lora_name]

    def _infer_full(self,
                    template: Template,
                    inputs: Dict[str, Any],
                    generation_config: GenerationConfig,
                    *,
                    lora_request: Optional[PtLoRARequest] = None) -> List[ChatCompletionResponse]:
        # bos_token TODO: encoder-decoder
        kwargs = {}
        if lora_request is not None:
            kwargs['adapter_names'] = self._get_adapter_names(lora_request)
        num_prompt_tokens = self._get_num_tokens(inputs)
        stopping_criteria = StoppingCriteriaList([StopWordsCriteria(self.tokenizer, generation_config.stop_words)])
        output = dict(
            self.model.generate(
                generation_config=generation_config, stopping_criteria=stopping_criteria, **inputs, **kwargs))
        batched_generate_ids = output['sequences']
        batched_generate_ids = template.get_generate_ids(batched_generate_ids, num_prompt_tokens)
        batched_logprobs = self.preprocess_logits(
            output.get('logits'), batched_generate_ids, generation_config.top_logprobs)

        res = []
        for i in range(batched_generate_ids.shape[0]):
            generate_ids = batched_generate_ids[i]

            # ignore pad_token
            masks = generate_ids != generation_config.pad_token_id
            generate_ids = generate_ids[masks].tolist()
            logprobs_list = None
            if batched_logprobs is not None:
                logprobs_list = [logprobs for m, logprobs in zip(masks, batched_logprobs[i]) if m.item()]

            logprobs = self._get_logprobs(self.tokenizer, logprobs_list, generate_ids, generation_config.top_logprobs)
            usage_info = self._get_usage_info(num_prompt_tokens, len(generate_ids))
            response = InferTools.safe_decode(template, generate_ids, True)

            toolcall = self._get_toolcall(response, True)
            choices = [
                ChatCompletionResponseChoice(
                    index=0,
                    message=ChatMessage(role='assistant', content=response, tool_calls=toolcall),
                    finish_reason=None,
                    logprobs=logprobs)
            ]
            res.append(ChatCompletionResponse(model=self.model_dir, choices=choices, usage=usage_info))
        return res

    @torch.inference_mode()
    async def infer_async(
        self,
        template: Template,
        infer_request: InferRequest,
        request_config: Optional[RequestConfig] = None,
        *,
        lora_request: Optional[PtLoRARequest] = None,
    ) -> Union[ChatCompletionResponse, AsyncIterator[ChatCompletionStreamResponse]]:
        infer_args = (template, infer_request, request_config)
        if request_config.stream:
            return self._infer_stream_async(*infer_args, lora_request=lora_request)
        else:
            return await self._infer_full_async(*infer_args, lora_request=lora_request)

    def _infer(
        self,
        template: Template,
        infer_requests: List[InferRequest],
        request_config: Optional[RequestConfig] = None,
        metrics: Optional[List[Metric]] = None,
        *,
        lora_request: Optional[PtLoRARequest] = None,
    ) -> Union[List[ChatCompletionResponse], Iterator[List[Optional[ChatCompletionStreamResponse]]]]:
        self.model.eval()
        request_config = request_config or RequestConfig()

        batched_inputs = []
        for infer_request in infer_requests:
            inputs = template.encode(infer_request)
            assert len(inputs) >= 0
            batched_inputs.append(inputs)
        inputs = to_device(
            template.data_collator(batched_inputs, padding_side='left'),
            next(self.model.parameters()).device)
        self.set_default_max_tokens(request_config, inputs)
        generation_config = self._prepare_generation_config(request_config)
        self._add_stop_words(generation_config, request_config, template)

        infer_args = (template, inputs, generation_config)
        if request_config.stream:
            return self._update_metrics_wrapper(self._infer_stream(*infer_args, lora_request=lora_request), metrics)
        else:
            return self._update_metrics(self._infer_full(*infer_args, lora_request=lora_request), metrics)

    @torch.inference_mode()
    def infer(
        self,
        template: Template,
        infer_requests: List[InferRequest],
        request_config: Optional[RequestConfig] = None,
        metrics: Optional[List[Metric]] = None,
        *,
        use_tqdm: Optional[bool] = None,
        max_batch_size: int = 16,  # TODO: async batch_size
        lora_request: Optional[PtLoRARequest] = None
    ) -> Union[List[ChatCompletionResponse], Iterator[List[Optional[ChatCompletionStreamResponse]]]]:

        if use_tqdm is None:
            use_tqdm = not request_config.stream
        prog_bar = tqdm(total=len(infer_requests), dynamic_ncols=True, disable=not use_tqdm)

        def _infer_full():
            res = []
            i = 0
            while i < len(infer_requests):
                infer_requests_samples = infer_requests[i:i + max_batch_size]
                res += self._infer(template, infer_requests_samples, request_config, metrics, lora_request=lora_request)
                i += max_batch_size
                prog_bar.update(len(infer_requests_samples))
            return res

        def _infer_stream() -> Iterator[List[Optional[ChatCompletionStreamResponse]]]:
            i = 0
            while i < len(infer_requests):
                infer_requests_samples = infer_requests[i:i + max_batch_size]
                gen = self._infer(template, infer_requests_samples, request_config, metrics, lora_request=lora_request)
                for response in gen:
                    res = [None] * len(infer_requests)
                    res[i:i + max_batch_size] = response
                    yield res
                i += max_batch_size
                prog_bar.update(len(infer_requests_samples))

        if request_config.stream:
            return _infer_stream()
        else:
            return _infer_full()
