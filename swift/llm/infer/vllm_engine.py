import inspect
import os
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Union

import torch
import vllm
from modelscope import GenerationConfig
from packaging import version
from transformers import PreTrainedTokenizerBase
from vllm import AsyncEngineArgs, AsyncLLMEngine, SamplingParams

from swift.plugin import Metric
from swift.utils import get_logger
from ..template import Template
from .base import InferEngine
from .patch import patch_auto_config, patch_auto_tokenizer
from .protocol import (ChatCompletionResponse, ChatCompletionResponseChoice, ChatCompletionResponseStreamChoice,
                       ChatCompletionStreamResponse, ChatMessage, DeltaMessage, InferRequest, RequestConfig, UsageInfo,
                       random_uuid)
from .utils import InferStreamer, InferTools

try:
    from vllm.lora.request import LoRARequest
except ImportError:
    pass

logger = get_logger()
dtype_mapping = {torch.float16: 'float16', torch.bfloat16: 'bfloat16', torch.float32: 'float32'}


class VllmEngine(InferEngine):

    def __init__(
            self,
            model_id_or_path: str,
            torch_dtype: Optional[torch.dtype] = None,
            *,
            model_type: Optional[str] = None,
            # engine_kwargs
            gpu_memory_utilization: float = 0.9,
            tensor_parallel_size: int = 1,
            max_num_seqs: int = 256,
            max_model_len: Optional[int] = None,
            disable_custom_all_reduce: bool = True,  # Default values different from vllm
            enforce_eager: bool = False,
            limit_mm_per_prompt: Optional[Dict[str, Any]] = None,
            # lora
            enable_lora: bool = False,
            max_loras: int = 1,
            max_lora_rank: int = 16,
            engine_kwargs: Optional[Dict[str, Any]] = None,  # extra
            **kwargs) -> None:
        self._init_env()
        self._prepare_model_tokenizer(model_id_or_path, torch_dtype, False, model_type=model_type, **kwargs)
        self._prepare_engine_kwargs(
            gpu_memory_utilization=gpu_memory_utilization,
            tensor_parallel_size=tensor_parallel_size,
            max_num_seqs=max_num_seqs,
            max_model_len=max_model_len,
            disable_custom_all_reduce=disable_custom_all_reduce,
            enforce_eager=enforce_eager,
            limit_mm_per_prompt=limit_mm_per_prompt,
            enable_lora=enable_lora,
            max_loras=max_loras,
            max_lora_rank=max_lora_rank,
            engine_kwargs=engine_kwargs)

        self._prepare_engine()
        self._load_generation_config()
        self._fix_vllm_bug()

    def _prepare_engine(self) -> None:
        with patch_auto_tokenizer(self.tokenizer), patch_auto_config(self.config):
            engine = AsyncLLMEngine.from_engine_args(self.engine_args)
        self.engine = engine

    def _prepare_engine_kwargs(
            self,
            gpu_memory_utilization: float = 0.9,
            tensor_parallel_size: int = 1,
            max_num_seqs: int = 256,
            max_model_len: Optional[int] = None,
            disable_custom_all_reduce: bool = True,  # Default values different from vllm
            enforce_eager: bool = False,
            limit_mm_per_prompt: Optional[Dict[str, Any]] = None,
            enable_lora: bool = False,
            max_loras: int = 1,
            max_lora_rank: int = 16,
            engine_kwargs: Optional[Dict[str, Any]] = None) -> AsyncEngineArgs:
        if engine_kwargs is None:
            engine_kwargs = {}
        disable_log_stats = engine_kwargs.pop('disable_log_stats', True)
        engine_kwargs['disable_log_requests'] = True

        parameters = inspect.signature(AsyncEngineArgs.__init__).parameters
        if 'enable_lora' in parameters and enable_lora:
            engine_kwargs['enable_lora'] = enable_lora
            engine_kwargs['max_loras'] = max_loras
            engine_kwargs['max_lora_rank'] = max_lora_rank
        else:
            assert not enable_lora, 'The current version of vLLM does not support `enable_lora`. Please upgrade vLLM.'

        if 'limit_mm_per_prompt' in parameters and limit_mm_per_prompt:
            engine_kwargs['limit_mm_per_prompt'] = limit_mm_per_prompt
        else:
            assert not limit_mm_per_prompt, (
                'The current version of VLLM does not support `limit_mm_per_prompt`. Please upgrade VLLM.')

        engine_args = AsyncEngineArgs(
            model=self.model_dir,
            dtype=dtype_mapping[self.torch_dtype],
            gpu_memory_utilization=gpu_memory_utilization,
            tensor_parallel_size=tensor_parallel_size,
            max_num_seqs=max_num_seqs,
            max_model_len=max_model_len,
            disable_log_stats=disable_log_stats,
            disable_custom_all_reduce=disable_custom_all_reduce,
            enforce_eager=enforce_eager,
            trust_remote_code=True,
            **engine_kwargs)
        self.engine_args = engine_args
        self.enable_lora = enable_lora
        if max_model_len is not None:
            self.max_model_len = max_model_len

    @staticmethod
    def _init_env() -> None:
        try:
            from vllm.model_executor.parallel_utils.parallel_state import destroy_model_parallel
            destroy_model_parallel()
        except ImportError:
            pass
        # fix HTTPError bug (use model_dir)
        os.environ.pop('VLLM_USE_MODELSCOPE', None)
        if version.parse(vllm.__version__) >= version.parse('0.5.1'):
            os.environ['VLLM_WORKER_MULTIPROC_METHOD'] = 'spawn'

    def _fix_vllm_bug(self) -> None:
        # fix vllm==0.4 bug (very slow)
        tokenizer = self.tokenizer
        if version.parse(
                vllm.__version__) >= version.parse('0.4') and not tokenizer.__class__.__name__.startswith('Cached'):
            _tokenizer_len = len(tokenizer)
            __old_len__ = tokenizer.__class__.__len__

            def __len__(self) -> int:
                if self is tokenizer:
                    return _tokenizer_len
                else:
                    return __old_len__(self)

            tokenizer.__class__.__len__ = __len__

    def _load_generation_config(self) -> None:
        generation_config_path = os.path.join(self.model_dir, 'generation_config.json')
        if os.path.isfile(generation_config_path):
            generation_config = GenerationConfig.from_pretrained(self.model_dir)
            kwargs = generation_config.to_dict()
            max_new_tokens = kwargs.get('max_new_tokens')
            if max_new_tokens is not None:
                kwargs['max_tokens'] = max_new_tokens
            parameters = inspect.signature(SamplingParams.__init__).parameters
            for k, v in kwargs.copy().items():
                if k not in parameters or v is None:
                    kwargs.pop(k)
            self.generation_config = SamplingParams(**kwargs)
        else:
            self.generation_config = SamplingParams()

    def _add_stop_words(self, generation_config: SamplingParams, request_config: RequestConfig,
                        template: Template) -> None:
        stop_words = (request_config.stop or []) + (self.generation_config.stop or []) + template.stop_words
        stop_words += [template.suffix[-1], self.tokenizer.eos_token]
        generation_config.stop = self._get_stop_words(stop_words)

    def _add_request(self,
                     inputs: Dict[str, Any],
                     generation_config: SamplingParams,
                     request_id: str,
                     lora_request: Optional['LoRARequest'] = None):
        kwargs = {}
        if self.enable_lora:
            kwargs['lora_request'] = lora_request
        input_ids = inputs['input_ids']
        if version.parse(vllm.__version__) >= version.parse('0.4.3'):
            llm_inputs = {'prompt_token_ids': input_ids}
            mm_data = {}
            for key in ['images', 'audios', 'videos']:
                media_data = inputs.get(key) or []
                if media_data:
                    if version.parse(vllm.__version__) < version.parse('0.6'):
                        assert len(media_data) == 1, (
                            f'The current version of vllm only supports single {key}. Please upgrade to vllm >= 0.6.0')
                        mm_data = {key.rstrip('s'): media_data[0]}
                    else:
                        mm_data = {key.rstrip('s'): media_data[0] if len(media_data) == 1 else media_data}
            if mm_data:
                llm_inputs['multi_modal_data'] = mm_data
            result_generator = self.engine.generate(llm_inputs, generation_config, request_id, **kwargs)
        else:
            result_generator = self.engine.generate(None, generation_config, request_id, input_ids, **kwargs)
        return result_generator

    @staticmethod
    def _get_logprobs(tokenizer: PreTrainedTokenizerBase,
                      logprobs_list: Optional[List[Dict[int, float]]],
                      token_ids: List[int],
                      top_logprobs: Optional[int] = None) -> Optional[Dict[str, Any]]:
        if logprobs_list is None:
            return None
        res = []
        for logprobs, token_id in zip(logprobs_list, token_ids):
            logprob = logprobs[token_id]
            chosen_token = tokenizer.decode(token_id)
            _res = {'token': chosen_token, 'logprob': logprob.logprob, 'bytes': list(chosen_token.encode('utf8'))}
            if top_logprobs is not None:
                res_top_logprobs = []
                for k, logprob in logprobs.items():
                    token = tokenizer.decode(k)
                    if logprob.logprob == float('-inf'):
                        continue
                    res_top_logprobs.append({
                        'token': token,
                        'logprob': logprob.logprob,
                        'bytes': list(token.encode('utf8'))
                    })
                _res['top_logprobs'] = res_top_logprobs
            res.append(_res)
        return {'content': res}

    def _prepare_generation_config(self, request_config: RequestConfig) -> SamplingParams:
        kwargs = {'max_tokens': request_config.max_tokens}
        for key in ['temperature', 'top_k', 'top_p', 'repetition_penalty']:
            new_value = getattr(request_config, key)
            if new_value is None:
                kwargs[key] = getattr(self.generation_config, key)
            else:
                kwargs[key] = new_value

        if request_config.logprobs:
            kwargs['logprobs'] = 1
            if request_config.top_logprobs is not None:
                kwargs['logprobs'] = max(1, request_config.top_logprobs)

        for key in ['n', 'best_of', 'frequency_penalty', 'length_penalty', 'presence_penalty', 'seed']:
            kwargs[key] = getattr(request_config, key)

        return SamplingParams(**kwargs)

    async def _infer_stream_async(self, template: Template, inputs: Dict[str, Any], generation_config: SamplingParams,
                                  **kwargs) -> AsyncIterator[ChatCompletionStreamResponse]:
        request_id = random_uuid()
        result_generator = self._add_request(inputs, generation_config, request_id, **kwargs)
        infer_streamers = [InferStreamer(template) for _ in range(generation_config.n)]
        async for result in result_generator:

            is_diff = False
            is_finished = False
            for output in result.outputs:
                output.delta_text = infer_streamers[output.index].get_printable_text(
                    output.token_ids, output.finished())
                output.is_finished = output.finish_reason is not None
                is_diff |= bool(output.delta_text)
                is_finished |= output.is_finished
            if not is_diff and not is_finished:
                continue

            num_generated_tokens = sum(len(output.token_ids) for output in result.outputs)
            usage_info = self._get_usage_info(len(result.prompt_token_ids), num_generated_tokens)
            choices = []
            for output in result.outputs:
                toolcall = self._get_toolcall(output.token_ids, output.is_finished)
                choice = ChatCompletionResponseStreamChoice(
                    index=output.index,
                    delta=DeltaMessage(role='assistant', content=output.delta_text, tool_calls=toolcall),
                    finish_reason=output.finish_reason)
                choices.append(choice)
            yield ChatCompletionStreamResponse(model=self.model_dir, choices=choices, usage=usage_info, id=request_id)

    async def _infer_full_async(self,
                                template: Template,
                                inputs: Dict[str, Any],
                                generation_config: SamplingParams,
                                lora_request: Optional['LoRARequest'] = None) -> ChatCompletionResponse:
        request_id = random_uuid()
        result_generator = self._add_request(inputs, generation_config, request_id, lora_request=lora_request)
        result = None
        async for result in result_generator:
            pass
        assert result is not None
        num_generated_tokens = sum(len(output.token_ids) for output in result.outputs)
        usage_info = self._get_usage_info(len(result.prompt_token_ids), num_generated_tokens)
        choices = []
        for output in result.outputs:
            response = InferTools.safe_decode(template, output.token_ids, True)
            logprobs = self._get_logprobs(template.tokenizer, output.logprobs, output.token_ids,
                                          generation_config.logprobs)
            toolcall = self._get_toolcall(response, True)
            choice = ChatCompletionResponseChoice(
                index=output.index,
                message=ChatMessage(role='assistant', content=response, tool_calls=toolcall),
                finish_reason=output.finish_reason,
                logprobs=logprobs)
            choices.append(choice)
        return ChatCompletionResponse(model=self.model_dir, choices=choices, usage=usage_info, id=request_id)

    @torch.inference_mode()
    def infer(
        self,
        template: Template,
        infer_requests: List[InferRequest],
        request_config: Optional[RequestConfig] = None,
        metrics: Optional[List[Metric]] = None,
        *,
        use_tqdm: Optional[bool] = None,
        lora_request: Optional['LoRARequest'] = None
    ) -> Union[List[ChatCompletionResponse], Iterator[List[ChatCompletionStreamResponse]]]:
        return super().infer(
            template, infer_requests, request_config, metrics, use_tqdm=use_tqdm, lora_request=lora_request)

    @torch.inference_mode()
    async def infer_async(
        self,
        template: Template,
        infer_request: InferRequest,
        request_config: Optional[RequestConfig] = None,
        *,
        lora_request: Optional['LoRARequest'] = None,
    ) -> Union[ChatCompletionResponse, AsyncIterator[ChatCompletionStreamResponse]]:
        return await super().infer_async(template, infer_request, request_config, lora_request=lora_request)