import torch
import torch.nn as nn

from .base import LycorisBaseModule
from ..utils.bnb import LinearNF4


class IA3Module(LycorisBaseModule):
    support_module = {
        "linear",
        "conv1d",
        "conv2d",
        "conv3d",
    }

    def __init__(
        self,
        lora_name,
        org_module: nn.Module,
        multiplier=1.0,
        lora_dim=4,
        alpha=1,
        dropout=0.0,
        rank_dropout=0.0,
        module_dropout=0.0,
        use_tucker=False,
        use_scalar=False,
        rank_dropout_scale=False,
        weight_decompose=False,
        bypass_mode=False,
        rs_lora=False,
        train_on_input=False,
        **kwargs,
    ):
        """if alpha == 0 or None, alpha is rank (no scaling)."""
        super().__init__(
            lora_name,
            org_module,
            multiplier,
            dropout,
            rank_dropout,
            module_dropout,
            rank_dropout_scale,
            bypass_mode,
        )
        if self.module_type not in self.support_module:
            raise ValueError(f"{self.module_type} is not supported in IA^3 algo.")
        self.lora_dim = lora_dim
        self.tucker = False
        self.rs_lora = rs_lora

        self.shape = org_module.weight.shape
        if self.module_type.startswith("conv"):
            self.isconv = True
            in_dim = org_module.in_channels
            out_dim = org_module.out_channels
            if train_on_input:
                train_dim = in_dim
            else:
                train_dim = out_dim
            self.weight = nn.Parameter(
                torch.empty(1, train_dim, *(1 for _ in self.shape[2:]))
            )
        else:
            in_dim = org_module.in_features
            out_dim = org_module.out_features
            if train_on_input:
                train_dim = in_dim
            else:
                train_dim = out_dim

            self.weight = nn.Parameter(torch.empty(train_dim))

        # Need more experiences on init method
        torch.nn.init.constant_(self.weight, 0)

        self.multiplier = multiplier
        self.train_input = train_on_input
        self.register_buffer("on_input", torch.tensor(int(train_on_input)))

    def apply_to(self):
        self.org_forward = self.org_module[0].forward
        self.org_module[0].forward = self.forward

    def make_weight(self, multiplier=1, shape=None, device=None, diff=False):
        weight = self.weight * multiplier + int(not diff)
        if self.train_input:
            diff = self.org_weight * weight
        else:
            diff = self.org_weight.transpose(0, 1) * weight
            diff = diff.transpose(0, 1)
        if shape is not None:
            diff = diff.view(shape)
        if device is not None:
            diff = diff.to(device)
        return diff

    def get_diff_weight(self, multiplier=1, shape=None, device=None):
        diff = self.make_weight(
            multiplier=multiplier, shape=shape, device=device, diff=True
        )
        return diff, None

    def get_merged_weight(self, multiplier=1, shape=None, device=None):
        diff = self.make_weight(multiplier=multiplier, shape=shape, device=device)
        return diff, None

    def _bypass_forward(self, x, scale=1, diff=False):
        weight = self.weight * scale + int(not diff)
        if self.train_input:
            x = x * weight
        out = self.org_forward(x)
        if not self.train_input:
            out = out * weight
        return out

    def bypass_forward_diff(self, x, scale=1):
        return self._bypass_forward(x, scale, diff=True)

    def bypass_forward(self, x, scale=1):
        return self._bypass_forward(x, scale, diff=False)

    def forward(self, x, *args, **kwargs):
        if self.module_dropout and self.training:
            if torch.rand(1) < self.module_dropout:
                return self.org_forward(x)
        if self.bypass_mode:
            return self.bypass_forward(x, self.multiplier)
        else:
            weight = self.get_merged_weight(multiplier=self.multiplier)[0]
            bias = (
                None
                if self.org_module[0].bias is None
                else self.org_module[0].bias.data
            )
            return self.op(x, weight, bias, **self.kw_dict)


if __name__ == "__main__":
    device = torch.device("cuda")
    module = IA3Module
    with torch.autocast("cuda" if torch.cuda.is_available() else "cpu"):
        base = nn.Linear(128, 128).to(device).half()
        net = module("test", base, 1, 4, 1, weight_decompose=True).to(device)
        print(net)
        test_input = torch.randn(1, 128).to(device).half()
        test_output = net(test_input)
        torch.sum(test_output).backward()
        print(test_output.shape)

        base_4bit = LinearNF4(128, 128, device="cuda")
        base_4bit.load_state_dict(base.state_dict())
        base_4bit.to(device)
        qnet = module("test", base_4bit, 1, 4, 1, weight_decompose=False).to(device)
        print(qnet)
        test_input = torch.randn(1, 128).to(device).half()
        test_output = qnet(test_input)
        torch.sum(test_output).backward()
        print(test_output.shape)

        base = nn.Conv2d(128, 128, 3, 1, 1).to(device).half()
        net = module("test", base, 1, 4, 1, weight_decompose=True, use_tucker=True).to(
            device
        )
        print(net)
        test_input = torch.randn(1, 128, 16, 16).to(device).half()
        test_output = net(test_input)
        torch.sum(test_output).backward()
        print(test_output.shape)

        base = nn.Conv2d(128, 128, 3, 1, 1).to(device).half()
        net = module.parametrize(
            base, "weight", 1, 4, 1, weight_decompose=True, use_tucker=True
        ).to(device)
        print(base)
        test_input = torch.randn(1, 128, 16, 16).to(device).half()
        test_output = base(test_input)
        torch.sum(test_output).backward()
        print(test_output.shape)
