import os.path
import time
from typing import Tuple

import torch
import numpy as np

from plyfile import PlyData, PlyElement

import internal.utils.colmap as colmap_utils
from internal.cameras.cameras import Cameras
from internal.dataparsers.dataparser import DataParser, ImageSet, DataParserOutputs
from internal.configs.dataset import ColmapParams
from internal.utils.graphics_utils import store_ply, fetch_ply, BasicPointCloud, getNerfppNorm


class ColmapDataParser(DataParser):
    def __init__(self, path: str, output_path: str, global_rank: int, params: ColmapParams) -> None:
        super().__init__()
        self.path = path
        self.output_path = output_path
        self.global_rank = global_rank
        self.params = params

    def detect_sparse_model_dir(self) -> str:
        if os.path.isdir(os.path.join(self.path, "sparse", "0")):
            return os.path.join(self.path, "sparse", "0")
        return os.path.join(self.path, "sparse")

    def get_image_dir(self) -> str:
        if self.params.image_dir is None:
            return os.path.join(self.path, "images")
        return os.path.join(self.path, self.params.image_dir)

    @staticmethod
    def read_points3D_binary(path_to_model_file):
        """
        see: src/base/reconstruction.cc
            void Reconstruction::ReadPoints3DBinary(const std::string& path)
            void Reconstruction::WritePoints3DBinary(const std::string& path)
        """

        with open(path_to_model_file, "rb") as fid:
            num_points = colmap_utils.read_next_bytes(fid, 8, "Q")[0]

            xyzs = np.empty((num_points, 3))
            rgbs = np.empty((num_points, 3))
            errors = np.empty((num_points, 1))

            for p_id in range(num_points):
                binary_point_line_properties = colmap_utils.read_next_bytes(
                    fid, num_bytes=43, format_char_sequence="QdddBBBd")
                xyz = np.array(binary_point_line_properties[1:4])
                rgb = np.array(binary_point_line_properties[4:7])
                error = np.array(binary_point_line_properties[7])
                track_length = colmap_utils.read_next_bytes(
                    fid, num_bytes=8, format_char_sequence="Q")[0]
                track_elems = colmap_utils.read_next_bytes(
                    fid, num_bytes=8 * track_length,
                    format_char_sequence="ii" * track_length)
                xyzs[p_id] = xyz
                rgbs[p_id] = rgb
                errors[p_id] = error
        return xyzs, rgbs, errors

    @staticmethod
    def convert_points_to_ply(path, xyz, rgb):
        store_ply(path, xyz, rgb)

    def get_outputs(self) -> DataParserOutputs:
        # load colmap sparse model
        sparse_model_dir = self.detect_sparse_model_dir()
        cameras = colmap_utils.read_cameras_binary(os.path.join(sparse_model_dir, "cameras.bin"))
        images = colmap_utils.read_images_binary(os.path.join(sparse_model_dir, "images.bin"))

        # sort images
        images = dict(sorted(images.items(), key=lambda item: item[0]))

        image_dir = self.get_image_dir()

        # convert points3D to ply
        ply_path = os.path.join(sparse_model_dir, "points3D.ply")
        while os.path.exists(ply_path) is False:
            if self.global_rank == 0:
                print("converting points3D.bin to ply format")
                xyz, rgb, _ = ColmapDataParser.read_points3D_binary(os.path.join(sparse_model_dir, "points3D.bin"))
                ColmapDataParser.convert_points_to_ply(ply_path + ".tmp", xyz=xyz, rgb=rgb)
                os.rename(ply_path + ".tmp", ply_path)
                break
            else:
                # waiting ply
                print("#{} waiting for {}".format(os.getpid(), ply_path))
                time.sleep(1)

        # initialize lists
        R_list = []
        T_list = []
        fx_list = []
        fy_list = []
        # fov_x_list = []
        # fov_y_list = []
        cx_list = []
        cy_list = []
        width_list = []
        height_list = []
        appearance_embedding_list = []
        camera_type_list = []
        image_name_list = []
        image_path_list = []
        mask_path_list = []

        # parse colmap sparse model
        for idx, key in enumerate(images):
            # extract image and its correspond camera
            extrinsics = images[key]
            intrinsics = cameras[extrinsics.camera_id]

            height = intrinsics.height
            width = intrinsics.width

            R = extrinsics.qvec2rotmat()
            T = np.array(extrinsics.tvec)

            if intrinsics.model == "SIMPLE_PINHOLE":
                focal_length_x = intrinsics.params[0]
                focal_length_y = focal_length_x
                cx = intrinsics.params[1]
                cy = intrinsics.params[2]
                # fov_y = focal2fov(focal_length_x, height)
                # fov_x = focal2fov(focal_length_x, width)
            elif intrinsics.model == "PINHOLE":
                focal_length_x = intrinsics.params[0]
                focal_length_y = intrinsics.params[1]
                cx = intrinsics.params[2]
                cy = intrinsics.params[3]
                # fov_y = focal2fov(focal_length_y, height)
                # fov_x = focal2fov(focal_length_x, width)
            else:
                assert False, "Colmap camera model not handled: only undistorted datasets (PINHOLE or SIMPLE_PINHOLE cameras) supported!"

            # whether mask exists
            mask_path = None
            if self.params.mask_dir is not None:
                mask_path = os.path.join(self.params.mask_dir, "{}.png".format(extrinsics.name))
                if os.path.exists(mask_path) is False:
                    mask_path = None

            # append data to list
            R_list.append(R)
            T_list.append(T)
            fx_list.append(focal_length_x)
            fy_list.append(focal_length_y)
            # fov_x_list.append(fov_x)
            # fov_y_list.append(fov_y)
            cx_list.append(cx)
            cy_list.append(cy)
            width_list.append(width)
            height_list.append(height)
            appearance_embedding_list.append(extrinsics.camera_id)  # TODO: support custom appearance embedding value
            camera_type_list.append(0)
            image_name_list.append(extrinsics.name)
            image_path_list.append(os.path.join(image_dir, extrinsics.name))
            mask_path_list.append(mask_path)

        # calculate norm
        norm = getNerfppNorm(R_list, T_list)

        # convert data to tensor
        R = torch.tensor(np.stack(R_list, axis=0), dtype=torch.float32)
        T = torch.tensor(np.stack(T_list, axis=0), dtype=torch.float32)
        fx = torch.tensor(fx_list, dtype=torch.float32)
        fy = torch.tensor(fy_list, dtype=torch.float32)
        # fov_x = torch.tensor(fov_x_list, dtype=torch.float32)
        # fov_y = torch.tensor(fov_y_list, dtype=torch.float32)
        cx = torch.tensor(cx_list, dtype=torch.float32)
        cy = torch.tensor(cy_list, dtype=torch.float32)
        width = torch.tensor(width_list, dtype=torch.int16)
        height = torch.tensor(height_list, dtype=torch.int16)
        appearance_embedding = torch.tensor(appearance_embedding_list, dtype=torch.float32)
        camera_type = torch.tensor(camera_type_list, dtype=torch.int8)

        # normalize appearance embedding
        appearance_embedding = appearance_embedding / torch.max(appearance_embedding)

        # TODO: reorient

        # build split indices
        if self.params.eval_step > 1:
            training_set_indices = []
            validation_set_indices = []
            for i in range(len(image_name_list)):
                if i % self.params.eval_step == 0:
                    validation_set_indices.append(i)
                else:
                    training_set_indices.append(i)
        else:
            training_set_indices = list(range(len(image_name_list)))
            validation_set_indices = [0]

        # split
        image_set = []
        for index_list in [training_set_indices, validation_set_indices]:
            indices = torch.tensor(index_list, dtype=torch.int)
            cameras = Cameras(
                R=R[indices],
                T=T[indices],
                fx=fx[indices],
                fy=fy[indices],
                cx=cx[indices],
                cy=cy[indices],
                width=width[indices],
                height=height[indices],
                appearance_embedding=appearance_embedding[indices],
                distortion_params=None,
                camera_type=camera_type[indices],
            )
            image_set.append(ImageSet(
                image_names=[image_name_list[i] for i in index_list],
                image_paths=[image_path_list[i] for i in index_list],
                mask_paths=[mask_path_list[i] for i in index_list],
                cameras=cameras
            ))

        return DataParserOutputs(
            train_set=image_set[0],
            val_set=image_set[1],
            test_set=image_set[1],
            point_cloud=fetch_ply(ply_path),
            ply_path=ply_path,
            camera_extent=norm["radius"],
        )