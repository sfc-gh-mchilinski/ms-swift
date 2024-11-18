from typing import Any, Dict, List, Union

from ..argument import RLHFArguments
from .sft import SwiftSft


class SwiftRLHF(SwiftSft[RLHFArguments]):
    args_class = RLHFArguments

    def _prepare_model_tokenizer(self):
        args = self.args
        self.ref_model = None
        if args.ref_model:
            # Be aware of the unexpected behavior caused by double monkey patching.
            self.ref_model, _ = self._get_model_tokenizer(args.ref_model, args.ref_model_type, args.ref_model_revision)
            self.ref_model.requires_grad_(False).eval()

        super()._prepare_model_tokenizer()

    def _register_post_encode_hook(self):
        models = [self.model]
        if self.ref_model:
            models.append(self.ref_model)
        template.register_post_encode_hook(models)


def rlhf_main(args: Union[List[str], RLHFArguments, None] = None) -> List[Dict[str, Any]]:
    return SwiftRLHF(args).main()
