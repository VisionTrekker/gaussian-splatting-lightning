from typing import Tuple, Optional, Any, List, Dict, Mapping
from dataclasses import dataclass, field
import lightning
import torch
from torch import nn

from . import RendererOutputInfo, RendererOutputTypes
from .renderer import Renderer, RendererConfig
from internal.utils.network_factory import NetworkFactory
from ..cameras import Camera
from ..models.gaussian import GaussianModel
from internal.encodings.positional_encoding import PositionalEncoding

from .gsplat_v1_renderer import GSplatV1Renderer, GSplatV1RendererModule, spherical_harmonics, spherical_harmonics_decomposed
from .gsplat_mip_splatting_renderer_v2 import MipSplattingRendererMixin


@dataclass
class ModelConfig:
    n_gaussian_feature_dims: int = 64
    n_appearances: int = -1
    n_appearance_embedding_dims: int = 32
    is_view_dependent: bool = False
    n_view_direction_frequencies: int = 4
    n_neurons: int = 64
    n_layers: int = 3
    skip_layers: List[int] = field(default_factory=lambda: [])

    normalize: bool = False

    tcnn: bool = False  # TODO: gradient scaling
    """Speed up a little, but may sometimes reduce the metrics due to half-precision"""


@dataclass
class OptimizationConfig:
    gamma_eps: float = 1e-6

    embedding_lr_init: float = 2e-3
    embedding_lr_final_factor: float = 0.1
    lr_init: float = 1e-3
    lr_final_factor: float = 0.1
    eps: float = 1e-15
    max_steps: int = 30_000
    warm_up: int = 4000


class Model(nn.Module):
    def __init__(self, config: ModelConfig):
        super().__init__()
        self.config = config
        self._setup()

    def _setup(self):
        self.embedding = nn.Embedding(
            num_embeddings=self.config.n_appearances,
            embedding_dim=self.config.n_appearance_embedding_dims,
        )
        n_input_dims = self.config.n_gaussian_feature_dims + self.config.n_appearance_embedding_dims
        if self.config.is_view_dependent is True:
            self.view_direction_encoding = PositionalEncoding(3, self.config.n_view_direction_frequencies)
            n_input_dims += self.view_direction_encoding.get_output_n_channels()
        self.network = NetworkFactory(tcnn=self.config.tcnn).get_network_with_skip_layers(
            n_input_dims=n_input_dims,
            n_output_dims=3,
            n_layers=self.config.n_layers,
            n_neurons=self.config.n_neurons,
            activation="ReLU",
            output_activation="Sigmoid",
            skips=self.config.skip_layers,
        )

    def forward(self, gaussian_features, appearance, view_dirs):
        appearance_embeddings = self.embedding(appearance.reshape((-1,))).repeat(gaussian_features.shape[0], 1)
        if self.config.normalize:
            gaussian_features = torch.nn.functional.normalize(gaussian_features, dim=-1)
            appearance_embeddings = torch.nn.functional.normalize(appearance_embeddings, dim=-1)
        input_tensor_list = [gaussian_features, appearance_embeddings]
        if self.config.is_view_dependent is True:
            input_tensor_list.append(self.view_direction_encoding(view_dirs))
        network_input = torch.concat(input_tensor_list, dim=-1)
        return self.network(network_input)


@dataclass
class GSplatAppearanceEmbeddingRenderer(GSplatV1Renderer):
    separate_sh: bool = True

    model: ModelConfig = field(default_factory=lambda: ModelConfig())

    optimization: OptimizationConfig = field(default_factory=lambda: OptimizationConfig())

    def instantiate(self, *args, **kwargs) -> "GSplatAppearanceEmbeddingRendererModule":
        assert self.separate_sh is True

        if getattr(self, "model_config", None) is not None:
            # checkpoint generated by previous version
            self.model = self.config.model
            self.optimization = self.config.optimization

        return GSplatAppearanceEmbeddingRendererModule(self)


class GSplatAppearanceEmbeddingRendererModule(GSplatV1RendererModule):
    """
    rgb = f(point_features, appearance_embedding, view_direction)
    """

    def setup(self, stage: str, lightning_module=None, *args: Any, **kwargs: Any) -> Any:
        if lightning_module is not None:
            if self.config.model.n_appearances <= 0:
                max_input_id = 0
                appearance_group_ids = lightning_module.trainer.datamodule.dataparser_outputs.appearance_group_ids
                if appearance_group_ids is not None:
                    for i in appearance_group_ids.values():
                        if i[0] > max_input_id:
                            max_input_id = i[0]
                n_appearances = max_input_id + 1
                self.config.model.n_appearances = n_appearances

            self._setup_model()
            print(self.model)

    def _setup_model(self, device=None):
        self.model = Model(self.config.model)

        if device is not None:
            self.model.to(device=device)

    def load_state_dict(self, state_dict: Mapping[str, Any], strict: bool = True):
        self.config.model.n_appearances = state_dict["model.embedding.weight"].shape[0]
        self._setup_model(device=state_dict["model.embedding.weight"].device)
        return super().load_state_dict(state_dict, strict)

    def training_setup(self, module: lightning.LightningModule):
        embedding_optimizer, embedding_scheduler = self._create_optimizer_and_scheduler(
            self.model.embedding.parameters(),
            "embedding",
            lr_init=self.config.optimization.embedding_lr_init,
            lr_final_factor=self.config.optimization.lr_final_factor,
            max_steps=self.config.optimization.max_steps,
            eps=self.config.optimization.eps,
            warm_up=self.config.optimization.warm_up,
        )
        network_optimizer, network_scheduler = self._create_optimizer_and_scheduler(
            self.model.network.parameters(),
            "embedding_network",
            lr_init=self.config.optimization.lr_init,
            lr_final_factor=self.config.optimization.lr_final_factor,
            max_steps=self.config.optimization.max_steps,
            eps=self.config.optimization.eps,
            warm_up=self.config.optimization.warm_up,
        )

        return [embedding_optimizer, network_optimizer], [embedding_scheduler, network_scheduler]

    def sh(self, pc, dirs, mask=None):
        if pc.is_pre_activated:
            return spherical_harmonics(
                pc.active_sh_degree,
                dirs,
                pc.get_shs(),
                masks=mask,
            )
        return spherical_harmonics_decomposed(
            pc.active_sh_degree,
            dirs,
            dc=pc.get_shs_dc(),
            coeffs=pc.get_shs_rest(),
            masks=mask,
        )

    def selective_sh(self, pc, dirs, mask):
        if pc.is_pre_activated:
            return spherical_harmonics(
                pc.active_sh_degree,
                dirs,
                pc.get_shs()[mask],
            )
        return spherical_harmonics_decomposed(
            pc.active_sh_degree,
            dirs,
            dc=pc.get_shs_dc()[mask],
            coeffs=pc.get_shs_rest()[mask],
        )

    def get_rgbs(
        self,
        viewpoint_camera,
        pc,
        projections: Tuple,
        visibility_filter,
        status: Any,
        **kwargs,
    ):
        if kwargs.get("warm_up", False):
            return torch.clamp(
                self.sh(
                    pc,
                    pc.get_xyz.detach() - viewpoint_camera.camera_center,
                    visibility_filter,
                ) + 0.5,
                min=0.,
            )

        # calculate normalized view directions
        detached_xyz = pc.get_xyz.detach()[visibility_filter]
        view_directions = detached_xyz - viewpoint_camera.camera_center  # (N, 3)
        view_directions = torch.nn.functional.normalize(view_directions, dim=-1)

        base_rgbs = self.selective_sh(
            pc,
            view_directions,
            visibility_filter,
        ) + 0.5

        rgb_offsets = self.model(
            pc.get_appearance_features()[visibility_filter],
            viewpoint_camera.appearance_id,
            view_directions,
        ) * 2 - 1.

        means2d = projections[1]
        rgbs = torch.zeros((pc.n_gaussians, 3), dtype=means2d.dtype, device=means2d.device)
        rgbs[visibility_filter] = torch.clamp(
            base_rgbs + rgb_offsets,
            min=0.,
            max=1.,
        )

        return rgbs

    def training_forward(self, step: int, module: lightning.LightningModule, viewpoint_camera: Camera, pc: GaussianModel, bg_color: torch.Tensor, scaling_modifier=1.0, **kwargs):
        return self.forward(viewpoint_camera, pc, bg_color, scaling_modifier, warm_up=step < self.config.optimization.warm_up, **kwargs)

    @staticmethod
    def _create_optimizer_and_scheduler(
            params,
            name,
            lr_init,
            lr_final_factor,
            max_steps,
            eps,
            warm_up,
    ) -> Tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LRScheduler]:
        optimizer = torch.optim.Adam(
            params=[
                {"params": list(params), "name": name}
            ],
            lr=lr_init,
            eps=eps,
        )
        scheduler = torch.optim.lr_scheduler.LambdaLR(
            optimizer=optimizer,
            lr_lambda=lambda iter: lr_final_factor ** min(max(iter - warm_up, 0) / max_steps, 1),
            verbose=False,
        )

        return optimizer, scheduler


# With MipSplatting version

@dataclass
class GSplatAppearanceEmbeddingMipRenderer(GSplatAppearanceEmbeddingRenderer):
    filter_2d_kernel_size: float = 0.1

    def instantiate(self, *args, **kwargs) -> "GSplatAppearanceEmbeddingMipRendererModule":
        return GSplatAppearanceEmbeddingMipRendererModule(self)


class GSplatAppearanceEmbeddingMipRendererModule(MipSplattingRendererMixin, GSplatAppearanceEmbeddingRendererModule):
    pass
