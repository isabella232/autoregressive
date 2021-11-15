import argparse
import time

import matplotlib.pyplot as plt
import torch
import torch.utils.data
import pytorch_lightning as pl
from mpl_toolkits.axes_grid1 import ImageGrid
from pytorch_lightning.utilities.cli import LightningCLI


from .. import wave, generators


class InstantiateOnlyLightningCLI(LightningCLI):
    def fit(self) -> None:
        return None

    def add_arguments_to_parser(self, parser) -> None:
        parser.link_arguments(
            "data.quantization_levels",
            "model.quantization_levels",
            apply_on="instantiate",
        )
        return super().add_arguments_to_parser(parser)


class ForecastLightningCLI(InstantiateOnlyLightningCLI):
    def add_arguments_to_parser(self, parser) -> None:
        parser.add_argument("-horizon", type=int, default=None)
        parser.add_argument("-num-obs", type=int)
        parser.add_argument("-num-curves", type=int, default=4)
        parser.add_argument(
            "--fast-wavenet", action=argparse.BooleanOptionalAction, default=True
        )
        parser.add_argument("ckpt", type=str)
        return super().add_arguments_to_parser(parser)


def load_model(cli: ForecastLightningCLI) -> wave.WaveNet:
    model: wave.WaveNet = cli.model
    cfg = cli.config
    checkpoint = torch.load(cfg["ckpt"])
    model.load_state_dict(checkpoint["state_dict"])
    return model.eval()


def create_obs(cli: ForecastLightningCLI) -> torch.Tensor:
    dm = cli.datamodule
    dl = dm.val_dataloader()
    series_batch, _ = next(iter(dl))
    return series_batch["x"][: cli.config["num_curves"]]


def create_fig(num_curves: int):
    fig = plt.figure()
    grid = ImageGrid(
        fig=fig,
        rect=111,
        nrows_ncols=(num_curves, 1),
        axes_pad=0.05,
        share_all=True,
        # label_mode="1",
        aspect=False,
    )
    # grid[0].get_yaxis().set_ticks([])
    # grid[0].get_xaxis().set_ticks([])
    return fig, grid


@torch.no_grad()
def main():
    cli = ForecastLightningCLI(
        wave.WaveNet,
        pl.LightningDataModule,
        subclass_mode_model=False,
        subclass_mode_data=True,
    )

    cfg = cli.config
    dev = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    dm = cli.datamodule
    model = load_model(cli).to(dev)
    sampler = model.create_sampler()
    obs = create_obs(cli).to(dev)

    num_obs = cfg["num_obs"] or model.receptive_field
    horizon = cfg["horizon"] or model.receptive_field * 2
    t = torch.arange(0, max(num_obs + horizon, obs.shape[-1]))

    # Generate. Note, we leave the last obs out so the first item predicted
    # overlaps the last observation and we hence get a smooth plot
    if cfg["fast_wavenet"]:
        g = generators.generate_fast(
            model=model,
            initial_obs=obs[..., :num_obs],
            sampler=sampler,
        )
    else:
        g = generators.generate(
            model=model,
            initial_obs=obs[..., :num_obs],
            sampler=sampler,
        )
    t0 = time.time()
    trajs, _ = generators.slice_generator(g, stop=horizon)  # (B,T)
    print(f"Generation took {(time.time()-t0):.3f} secs")

    # Plot
    fig, grid = create_fig(num_curves=obs.shape[0])
    for idx, (ob, traj, ax) in enumerate(zip(obs, trajs, grid)):
        ax.step(
            t[: ob.shape[-1]],
            ob.cpu(),
            linewidth=0.5,
            label="input",
            linestyle="--",
            alpha=0.8,
            c="k",
        )
        ax.step(
            t[num_obs : num_obs + horizon],
            traj.cpu(),
            label="generated" if idx == 0 else None,
        )
        ax.set_ylim(0, model.quantization_levels)

    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2)
    fig.tight_layout()
    fig.savefig(f"tmp/forecast_{type(model).__name__}.svg", bbox_inches="tight")
    plt.show()


if __name__ == "__main__":
    # python -m autoregressive.scripts.forecast --config models\fseries\config.yaml "models\fseries\wavenet-epoch=16-val_loss_epoch=4.9223.ckpt"
    main()
