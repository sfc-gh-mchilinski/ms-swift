# Copyright (c) Alibaba, Inc. and its affiliates.
import os
import sys
from typing import Dict, Any

from modelscope import AutoTokenizer
from transformers import PretrainedConfig

from swift.llm import TemplateType
from .. import LLMModelType, MLLMModelType
from ..register import (register_model, get_model_tokenizer_with_flash_attn, Model, ModelGroup, ModelMeta)
from ..utils import git_clone_github
from swift.utils import get_logger

logger = get_logger()


def get_model_tokenizer_yi1_5(model_dir, *args, **kwargs):
    tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True, use_fast=False)
    return get_model_tokenizer_with_flash_attn(model_dir, *args, tokenizer=tokenizer, **kwargs)


def get_model_tokenizer_yi_vl(model_dir: str,
                              config: PretrainedConfig,
                              model_kwargs: Dict[str, Any],
                              load_model: bool = True,
                              **kwargs):
    if 'local_repo_path' in kwargs:
        local_repo_path = kwargs['local_repo_path']
    else:
        local_repo_path = git_clone_github('https://github.com/01-ai/Yi')
    sys.path.append(os.path.join(local_repo_path, 'VL'))
    from llava.model import LlavaLlamaForCausalLM, LlavaConfig
    from llava.model.constants import key_info

    model_config = LlavaConfig.from_pretrained(model_dir)
    mm_vision_tower = model_config.mm_vision_tower
    model_config.mm_vision_tower = os.path.join(model_dir, *mm_vision_tower.rsplit('/', maxsplit=2)[-2:])
    model_config.attention_dropout = 0.
    key_info['model_path'] = model_dir
    model, tokenizer = get_model_tokenizer_with_flash_attn(
        model_dir,
        model_config,
        model_kwargs,
        load_model,
        model_config=model_config,
        automodel_class=LlavaLlamaForCausalLM,
        **kwargs)
    if model is not None:
        logger.info('Please ignore the above warning.')
        logger.info('Loading the parameters of vision_tower...')
        model.resize_token_embeddings(len(tokenizer))
        vision_tower = model.get_vision_tower()
        vision_tower.load_model()
        vision_tower.to(device=model.device, dtype=model_config.torch_dtype)
        if not hasattr(model.config, 'max_sequence_length'):
            model.config.max_sequence_length = 2048
        tokenizer.image_processor = vision_tower.image_processor
    return model, tokenizer


register_model(
    ModelMeta(
        MLLMModelType.yi_vl,
        [
            ModelGroup(
                [
                    Model('01ai/Yi-VL-34B', '01-ai/Yi-VL-34B'),
                    Model('01ai/Yi-VL-6B', '01-ai/Yi-VL-6B'),
                ], requires=['transformers>=4.34'], tags=['multi-modal', 'vision'],),
        ],
        TemplateType.yi_vl,
        get_model_tokenizer_yi_vl,
        support_flash_attn=True,
        architectures=['LlavaForConditionalGeneration'],
    ))


def get_model_tokenizer_yi(model_dir, *args, **kwargs):
    tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True, use_fast=False)
    return get_model_tokenizer_with_flash_attn(model_dir, *args, tokenizer=tokenizer, **kwargs)


register_model(
    ModelMeta(
        LLMModelType.yi,
        [  # yi
            ModelGroup([
                Model('01ai/Yi-6B', '01-ai/Yi-6B'),
                Model('01ai/Yi-6B-200K', '01-ai/Yi-6B-200K'),
                Model('01ai/Yi-6B-Chat', '01-ai/Yi-6B-Chat'),
                Model('01ai/Yi-6B-Chat-4bits', '01-ai/Yi-6B-Chat-4bits'),
                Model('01ai/Yi-6B-Chat-8bits', '01-ai/Yi-6B-Chat-8bits'),
                Model('01ai/Yi-9B', '01-ai/Yi-9B'),
                Model('01ai/Yi-9B-200K', '01-ai/Yi-9B-200K'),
                Model('01ai/Yi-34B', '01-ai/Yi-34B'),
                Model('01ai/Yi-34B-200K', '01-ai/Yi-34B-200K'),
                Model('01ai/Yi-34B-Chat', '01-ai/Yi-34B-Chat'),
                Model('01ai/Yi-34B-Chat-4bits', '01-ai/Yi-34B-Chat-4bits'),
                Model('01ai/Yi-34B-Chat-8bits', '01-ai/Yi-34B-Chat-8bits'),
            ], TemplateType.chatml),
            # yi1.5
            ModelGroup([
                Model('01ai/Yi-1.5-6B', '01-ai/Yi-1.5-6B'),
                Model('01ai/Yi-1.5-6B-Chat', '01-ai/Yi-1.5-6B-Chat'),
                Model('01ai/Yi-1.5-9B', '01-ai/Yi-1.5-9B'),
                Model('01ai/Yi-1.5-9B-Chat', '01-ai/Yi-1.5-9B-Chat'),
                Model('01ai/Yi-1.5-9B-Chat-16K', '01-ai/Yi-1.5-9B-Chat-16K'),
                Model('01ai/Yi-1.5-34B', '01-ai/Yi-1.5-34B'),
                Model('01ai/Yi-1.5-34B-Chat', '01-ai/Yi-1.5-34B-Chat'),
                Model('01ai/Yi-1.5-34B-Chat-16K', '01-ai/Yi-1.5-34B-Chat-16K'),
            ], TemplateType.chatml),
            # yi1.5-quant
            ModelGroup([
                Model('AI-ModelScope/Yi-1.5-6B-Chat-GPTQ', 'modelscope/Yi-1.5-6B-Chat-GPTQ'),
                Model('AI-ModelScope/Yi-1.5-6B-Chat-AWQ', 'modelscope/Yi-1.5-6B-Chat-AWQ'),
                Model('AI-ModelScope/Yi-1.5-9B-Chat-GPTQ', 'modelscope/Yi-1.5-9B-Chat-GPTQ'),
                Model('AI-ModelScope/Yi-1.5-9B-Chat-AWQ', 'modelscope/Yi-1.5-9B-Chat-AWQ'),
                Model('AI-ModelScope/Yi-1.5-34B-Chat-GPTQ', 'modelscope/Yi-1.5-34B-Chat-GPTQ'),
                Model('AI-ModelScope/Yi-1.5-34B-Chat-AWQ', 'modelscope/Yi-1.5-34B-Chat-AWQ'),
            ]),
        ],
        TemplateType.chatml,
        get_model_tokenizer_yi,
        architectures=['LlamaForCausalLM'],
        support_flash_attn=True,
        support_vllm=True,
        support_lmdeploy=True,
    ))

register_model(
    ModelMeta(
        LLMModelType.yi_coder,
        [
            ModelGroup([
                Model('01ai/Yi-Coder-1.5B', '01-ai/Yi-Coder-1.5B'),
                Model('01ai/Yi-Coder-9B', '01-ai/Yi-Coder-9B'),
                Model('01ai/Yi-Coder-1.5B-Chat', '01-ai/Yi-Coder-1.5B-Chat'),
                Model('01ai/Yi-Coder-9B-Chat', '01-ai/Yi-Coder-9B-Chat'),
            ],
                       tags=['coding'])
        ],
        TemplateType.yi_coder,
        get_model_tokenizer_yi,
        architectures=['LlamaForCausalLM'],
        support_flash_attn=True,
        support_vllm=True,
        support_lmdeploy=True,
    ))
