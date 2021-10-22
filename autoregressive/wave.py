from typing import Callable, Iterator, List, Optional, Protocol

import torch
import torch.nn
import torch.nn.functional as F
import torch.nn.init

from .utils import causal_pad

FastQueues = List[torch.FloatTensor]


def wave_init_weights(m):
    """Initialize conv1d with Xavier_uniform weight and 0 bias."""
    if isinstance(m, torch.nn.Conv1d):
        torch.nn.init.xavier_uniform_(m.weight)
        torch.nn.init.constant_(m.bias, 0.0)


class WaveNetLayer(torch.nn.Module):
    def __init__(
        self,
        dilation: int,
        residual_channels: int = 32,
        skip_channels: int = 32,
    ):
        super().__init__()
        self.dilation = dilation
        self.residual_channels = residual_channels
        self.conv_dilation = torch.nn.Conv1d(
            residual_channels,
            residual_channels,
            kernel_size=2,
            dilation=dilation,
        )
        self.conv_tanh = torch.nn.Conv1d(
            residual_channels,
            residual_channels,
            kernel_size=1,
        )
        self.conv_sig = torch.nn.Conv1d(
            residual_channels,
            residual_channels,
            kernel_size=1,
        )
        self.conv_skip = torch.nn.Conv1d(
            residual_channels,
            skip_channels,
            kernel_size=1,
        )
        self.conv_residual = torch.nn.Conv1d(
            residual_channels,
            residual_channels,
            kernel_size=1,
        )

    def forward(self, x, fast: bool = False):
        """
        When fast is enabled, this function assumes that x is composed
        of the last 'recurrent' input, that is `dilation` steps back and
        the current input.
        """
        if fast:
            x_dilated = F.conv1d(
                x,
                self.conv_dilation.weight,
                self.conv_dilation.bias,
                dilation=1,
            )
        else:
            x_dilated = self.conv_dilation(
                causal_pad(x, 2, self.dilation),
            )
        x_filter = torch.tanh(self.conv_tanh(x_dilated))
        x_gate = torch.sigmoid(self.conv_sig(x_dilated))
        x_h = x_gate * x_filter
        skip = self.conv_skip(x_h)
        return x_h + x_dilated, skip


class WaveNet(torch.nn.Module):
    def __init__(
        self,
        in_channels: int = 1,
        out_channels: int = 1,
        residual_channels: int = 32,
        skip_channels: int = 32,
        num_blocks: int = 1,
        num_layers_per_block: int = 7,
    ):
        super().__init__()
        self.conv_input = torch.nn.Conv1d(in_channels, residual_channels, kernel_size=1)
        self.wave_layers = torch.nn.ModuleList(
            [
                WaveNetLayer(
                    dilation=2 ** d,
                    residual_channels=residual_channels,
                    skip_channels=skip_channels,
                )
                for _ in range(num_blocks)
                for d in range(num_layers_per_block)
            ]
        )
        self.conv_mid = torch.nn.Conv1d(skip_channels, skip_channels, kernel_size=1)
        self.conv_output = torch.nn.Conv1d(skip_channels, out_channels, kernel_size=1)
        kernel_size = 2
        self.receptive_field = (kernel_size - 1) * sum(
            [2 ** i for _ in range(num_blocks) for i in range(num_layers_per_block)]
        ) + 1
        self.residual_channels = residual_channels
        self.skip_channels = skip_channels
        self.num_blocks = num_blocks
        self.num_layers_per_block = num_layers_per_block
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.apply(wave_init_weights)

    def encode(self, x):
        skips = []
        layer_inputs = []
        x = self.conv_input(x)
        for layer in self.wave_layers:
            layer_inputs.append(x)
            x, skip = layer(x, fast=False)
            skips.append(skip)
        return x, layer_inputs, skips

    def encode_one(self, x, queues: FastQueues):
        skips = []
        layer_inputs = []
        updated_queues = []
        x = self.conv_input(x)
        for layer, q in zip(self.wave_layers, queues):
            layer_inputs.append(x)
            x, qnew = self._next_input_from_queue(q, x)
            x, skip = layer(x, fast=True)
            updated_queues.append(qnew)
            skips.append(skip)
        return x, layer_inputs, skips, updated_queues

    def forward(self, x):
        e, layer_inputs, skips = self.encode(x)
        x = self._head(e, layer_inputs, skips)
        return x

    def forward_one(self, x, queues: FastQueues):
        e, layer_inputs, skips, queues = self.encode_one(x, queues)
        x = self._head(e, layer_inputs, skips)
        return x, queues

    def _head(self, encoded, layer_inputs, skips):
        del encoded, layer_inputs
        x = torch.stack(skips, dim=0).sum(dim=0)
        x = F.gelu(x)
        x = F.gelu(self.conv_mid(x))
        x = self.conv_output(x)
        return x

    def _next_input_from_queue(self, q: torch.Tensor, x):
        h = q[..., 0:1]  # pop left (oldest)
        qout = q.roll(-1, -1)  # roll by one in left direction
        qout[..., -1:] = x  # push right (newest)
        x = torch.cat((h, x), -1)  # prepare input
        return x, qout

    def create_empty_queues(
        self,
        device: Optional[torch.device] = None,
        dtype: torch.dtype = torch.float32,
        batch_size: int = 1,
    ) -> FastQueues:
        queues = []
        for layer in zip(self.wave_layers):
            layer: WaveNetLayer
            q = torch.zeros(
                (batch_size, layer.residual_channels, layer.dilation),
                dtype=dtype,
                device=device,
            )  # Populated with zeros will act like causal padding
            queues.append(q)
        return queues

    def create_initialized_queues(
        self, layer_inputs: Optional[List[torch.FloatTensor]]
    ) -> FastQueues:
        queues = []
        for layer, layer_input in zip(self.wave_layers, layer_inputs):
            layer: WaveNetLayer
            q = layer_input[..., -layer.dilation :]
            queues.append(q)
        return queues


class Sampler(Protocol):
    def __call__(
        self, model: WaveNet, obs: torch.Tensor, x: torch.Tensor
    ) -> torch.Tensor:
        ...


def regression_sampler(
    model: WaveNet, obs: torch.Tensor, x: torch.Tensor
) -> torch.Tensor:
    return x


def generate_fast(
    model: WaveNet, initial_obs: torch.Tensor, sampler: Sampler
) -> Iterator[torch.Tensor]:
    B, C, T = initial_obs.shape
    if T < 1:
        raise ValueError("Need at least one observation to bootstrap.")
    # prepare queues
    if T == 1:
        queues = model.create_empty_queues(
            device=initial_obs.device,
            dtype=initial_obs.dtype,
            batch_size=B,
        )
    else:
        _, layer_inputs, _ = model.encode(initial_obs[..., :-1])
        queues = model.create_initialized_queues(layer_inputs)
    # generate
    obs = initial_obs[..., -1:]
    while True:
        x, queues = model.forward_one(obs, queues)
        s = sampler(model, obs, x)
        yield s
        obs = s
