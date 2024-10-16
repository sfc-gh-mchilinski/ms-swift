# Copyright (c) Alibaba, Inc. and its affiliates.

import asyncio
import time
from queue import Queue
from threading import Thread
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Union

import torch
from tqdm import tqdm

from swift.plugin import Metric
from swift.utils import get_logger
from ..model import get_model_tokenizer
from ..template import Template, split_action_action_input
from .protocol import (ChatCompletionMessageToolCall, ChatCompletionResponse, ChatCompletionStreamResponse, Function,
                       InferRequest, RequestConfig, random_uuid)

logger = get_logger()


class InferEngine:

    def _prepare_model_tokenizer(self,
                                 model_id_or_path: str,
                                 torch_dtype: Optional[torch.dtype],
                                 load_model: bool,
                                 *,
                                 model_type: Optional[str] = None,
                                 **kwargs) -> None:
        use_hf = kwargs.pop('use_hf', None)
        revision = kwargs.pop('revision', None)
        model, tokenizer = get_model_tokenizer(
            model_id_or_path,
            torch_dtype,
            load_model=load_model,
            model_type=model_type,
            download_model=True,
            use_hf=use_hf,
            revision=revision)
        config = tokenizer.config
        self.config = config
        self.tokenizer = tokenizer
        self.model = model
        self.torch_dtype = config.torch_dtype

        self.model_type = config.model_type
        self.model_dir = config.model_dir
        self.is_multimodal = config.is_multimodal
        self.is_moe = config.is_moe
        self.chat_template = config.chat_template
        self.generation_template = config.generation_template
        self.max_model_len = config.max_model_len

    def _get_stop_words(self, stop_words: List[Union[str, List[int], None]]) -> List[str]:
        stop: List[str] = []
        for stop_word in stop_words:
            if stop_word is None:
                continue
            elif isinstance(stop_word, list):
                stop_word = self.tokenizer.decode(stop_word)
            assert isinstance(stop_word, str)
            if stop_word not in stop:
                stop.append(stop_word)
        return stop

    @staticmethod
    async def _run_infer(i, task, queue, stream: bool = False):
        # task with queue
        if stream:
            async for stream_response in await task:
                queue.put((i, stream_response))
        else:
            queue.put((i, await task))
        queue.put((i, None))

    @staticmethod
    async def _batch_run(tasks):
        return await asyncio.gather(*tasks)

    @staticmethod
    def _infer_stream(tasks,
                      stream: bool = True,
                      use_tqdm: bool = True,
                      metrics: Optional[List[Metric]] = None) -> Iterator[List[ChatCompletionStreamResponse]]:
        if metrics is None:
            metrics = []
        for metric in metrics:
            metric.reset()

        queue = Queue()
        new_tasks = [InferEngine._run_infer(i, task, queue, stream) for i, task in enumerate(tasks)]
        thread = Thread(target=lambda: asyncio.run(InferEngine._batch_run(new_tasks)))
        thread.start()

        prog_bar = tqdm(total=len(new_tasks), dynamic_ncols=True, disable=not use_tqdm)
        n_finished = 0
        outputs = [None] * len(new_tasks)

        while n_finished < len(new_tasks):
            i, output = queue.get()
            if output is None:  # is_finished
                n_finished += 1
                prog_bar.update()
            else:
                if outputs[i] is not None:  # The logic will only apply to the stream.
                    yield outputs
                    outputs = [None] * len(new_tasks)
                outputs[i] = output
            for metric in metrics:
                metric.update(output)
        yield outputs

    @staticmethod
    def _infer(tasks, use_tqdm: bool = True, metrics: Optional[List[Metric]] = None) -> List[ChatCompletionResponse]:
        for outputs in InferEngine._infer_stream(tasks, False, use_tqdm, metrics):
            pass
        return outputs

    @torch.inference_mode()
    def infer(self,
              template: Template,
              infer_requests: List[InferRequest],
              request_config: Optional[RequestConfig] = None,
              *,
              use_tqdm: Optional[bool] = None,
              metrics: Optional[List[Metric]] = None,
              **kwargs) -> Union[List[ChatCompletionResponse], Iterator[List[ChatCompletionStreamResponse]]]:
        tasks = [
            self.infer_async(template, infer_request, request_config, **kwargs) for infer_request in infer_requests
        ]
        if use_tqdm is None:
            use_tqdm = not request_config.stream
        if request_config.stream:
            return self._infer_stream(tasks, True, use_tqdm, metrics)
        else:
            return self._infer(tasks, use_tqdm, metrics)

    @staticmethod
    def _get_toolcall(response: str, is_finished: bool) -> Optional[List[ChatCompletionMessageToolCall]]:
        if is_finished:
            return None
        action, action_input = split_action_action_input(response)
        if action is None:
            return None

        return [
            ChatCompletionMessageToolCall(
                id=f'toolcall-{random_uuid()}', type='function', function=Function(name=action, arguments=action_input))
        ]

    @torch.inference_mode()
    async def infer_async(self,
                          template: Template,
                          infer_request: InferRequest,
                          request_config: Optional[RequestConfig] = None,
                          *,
                          request_id: Optional[str] = None,
                          **kwargs) -> Union[ChatCompletionResponse, AsyncIterator[ChatCompletionStreamResponse]]:
        if request_id is None:
            request_id = f'chatcmpl-{random_uuid()}'
        created_time = int(time.time())
        request_config = request_config or RequestConfig()

        inputs = template.encode(infer_request)
        assert len(inputs) >= 0
        self.set_default_max_tokens(request_config, inputs)
        generation_config = self._prepare_generation_config(request_config)
        self._add_stop_words(generation_config, request_config, template)
        infer_args = (template, inputs, generation_config, request_id, created_time)
        if request_config.stream:
            return self._infer_stream_async(*infer_args, **kwargs)
        else:
            return await self._infer_async(*infer_args, **kwargs)

    @staticmethod
    def _get_num_tokens(inputs: Dict[str, Any]) -> int:
        if 'input_ids' in inputs:  # 1d
            return len(inputs['input_ids'])
        elif 'inputs_embeds' in inputs:  # 2d
            return inputs['inputs_embeds'].shape[0]
        raise ValueError(f'Unable to retrieve input_ids and inputs_embeds. inputs: {inputs}')

    def set_default_max_tokens(self,
                               request_config: RequestConfig,
                               inputs: Dict[str, Any],
                               strict: bool = False) -> None:
        max_model_len = self.max_model_len
        num_tokens = InferEngine._get_num_tokens(inputs)
        max_tokens = request_config.max_tokens
        if max_model_len is None:
            max_model_len = 8192
            logger.warning(
                'The current model is unable to retrieve `max_model_len`. It is set to the default value of 8192.')
        max_max_tokens = max_model_len - num_tokens
        if max_tokens is None:
            request_config.max_tokens = max_max_tokens
        elif max_max_tokens < request_config.max_tokens:
            if strict:
                raise ValueError(
                    f'Your prompt has {num_tokens} tokens, and you have set the `max_tokens` to {max_tokens}, '
                    f'but the maximum model length supported is {max_model_len}. '
                    'Please reduce the number of tokens in the prompt or the `max_tokens`.')
            else:
                logger.warning(f'max_model_len({max_model_len}) - num_tokens({num_tokens}) < max_tokens({max_tokens}). '
                               f'Setting max_tokens: {max_model_len - num_tokens}')
                request_config.max_tokens = max_max_tokens