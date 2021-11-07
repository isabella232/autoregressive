import itertools
from typing import Protocol, List, TYPE_CHECKING, Iterator, Tuple

import torch

if TYPE_CHECKING:
    from .wave import WaveNet

from . import fast

WaveGenerator = Iterator[Tuple[torch.Tensor, torch.Tensor]]


class ObservationSampler(Protocol):
    def __call__(self, logits: torch.Tensor) -> torch.Tensor:
        ...


def generate(
    model: "WaveNet",
    initial_obs: torch.Tensor,
    sampler: ObservationSampler,
) -> WaveGenerator:
    B, Q, T = initial_obs.shape
    if T < 1:
        raise ValueError("Need at least one observation to bootstrap.")

    # We need to track up to the last n samples,
    # where n equals the receptive field of the model
    R = model.receptive_field
    history = initial_obs.new_zeros(
        (B, Q, R)
    )  # TODO C=Q, should maybe not be part of sampler, except if one_hot is True
    t = min(R, T)
    history[..., :t] = initial_obs[..., -t:]

    while True:
        obs = history[..., :t]  # (B,Q,T)
        logits = model.forward(obs)  # (B,Q,T)
        s = sampler(logits[..., -1:])  # yield sample for t+1 only (B,Q,1)
        yield s, logits[..., -1:]
        roll = int(t == R)
        history = history.roll(-roll, -1)  # no-op as long as history is not full
        t = min(t + 1, R)
        history[..., t - 1 : t] = s


def generate_fast(
    model: "WaveNet",
    initial_obs: torch.Tensor,
    sampler: ObservationSampler,
    layer_inputs: List[torch.Tensor] = None,
) -> WaveGenerator:
    B, _, T = initial_obs.shape
    if T < 1:
        raise ValueError("Need at least one observation to bootstrap.")
    # prepare queues
    if T == 1:
        queues = fast.create_empty_queues(
            model=model,
            device=initial_obs.device,
            dtype=initial_obs.dtype,
            batch_size=B,
        )
    else:
        if layer_inputs is None:
            _, layer_inputs, _ = model.encode(
                initial_obs[..., :-1], remove_left_invalid=False
            )  # TODO we should encode only necessary inputs
        else:
            layer_inputs = [inp[..., :-1] for inp in layer_inputs]
        queues = fast.create_initialized_queues(model=model, layer_inputs=layer_inputs)
    # generate
    obs = initial_obs[..., -1:]  # (B,Q,1)
    while True:
        logits, queues = model.forward_one(obs, queues)
        s = sampler(logits)  # (B,Q,1)
        yield s, logits
        obs = s


def slice_generator(
    gen: WaveGenerator,
    stop: int,
    step: int = 1,
    start: int = 0,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Slices the given generator to get subsequent predictions and network outputs."""
    sl = itertools.islice(gen, start, stop, step)  # List[(sample,output)]
    samples, outputs = list(zip(*sl))
    return torch.cat(samples, -1), torch.cat(outputs, -1)
