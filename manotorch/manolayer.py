import os
from collections import namedtuple
from typing import Optional
import warnings

import numpy as np
import torch
import lietorch
from .utils.quatutils import quaternion_to_rotation_matrix, quaternion_to_angle_axis

from mano.webuser.smpl_handpca_wrapper_HAND_only import ready_arguments

MANOOutput = namedtuple(
    "MANOOutput",
    [
        "verts",
        "joints",
        "center_idx",
        "center_joint",
        "full_poses",
        "betas",
        "transforms_abs",
    ],
)
MANOOutput.__new__.__defaults__ = (None,) * len(MANOOutput._fields)


def th_with_zeros(tensor):
    batch_size = tensor.shape[0]
    padding = tensor.new([0.0, 0.0, 0.0, 1.0])
    padding.requires_grad = False
    concat_list = [tensor, padding.view(1, 1, 4).repeat(batch_size, 1, 1)]
    cat_res = torch.cat(concat_list, 1)
    return cat_res


class ManoLayer(torch.nn.Module):
    def __init__(
        self,
        rot_mode: str = "axisang",
        side: str = "right",
        center_idx: Optional[int] = None,
        mano_assets_root: str = "assets/mano",
        use_pca: bool = False,
        flat_hand_mean: bool = True,  # Only used in pca mode
        ncomps: int = 15,  # Only used in pca mode
        **kargs,
    ):
        super().__init__()
        self.center_idx = center_idx
        self.rot_mode = rot_mode
        self.side = side
        self.use_pca = use_pca

        if rot_mode == "axisang":
            self.rotation_layer = self.rotation_by_axisang
            self.rot_dim = 3
        elif rot_mode == "quat":
            self.rotation_layer = self.rotation_by_quaternion
            self.rot_dim = 4
            if use_pca == True or flat_hand_mean == False:
                warnings.warn("Quat mode doesn't support PCA pose or flat_hand_mean !")
        else:
            raise NotImplementedError(f"Unrecognized rotation mode, expect [pca|axisang|quat], got {rot_mode}")

        # load model according to side flag
        mano_assets_path = os.path.join(mano_assets_root, f"MANO_{side.upper()}.pkl")  # eg.  MANO_RIGHT.pkl

        # parse and register stuff
        smpl_data = ready_arguments(mano_assets_path)
        self.register_buffer("th_betas", torch.Tensor(np.array(smpl_data["betas"].r)).unsqueeze(0))
        self.register_buffer("th_shapedirs", torch.Tensor(np.array(smpl_data["shapedirs"].r)))
        self.register_buffer("th_posedirs", torch.Tensor(np.array(smpl_data["posedirs"].r)))
        self.register_buffer("th_v_template", torch.Tensor(np.array(smpl_data["v_template"].r)).unsqueeze(0))
        self.register_buffer("th_J_regressor", torch.Tensor(np.array(smpl_data["J_regressor"].toarray())))
        self.register_buffer("th_weights", torch.Tensor(np.array(smpl_data["weights"].r)))
        self.register_buffer("th_faces", torch.Tensor(np.array(smpl_data["f"]).astype(np.int32)).long())

        kintree_table = smpl_data["kintree_table"]
        self.kintree_parents = list(kintree_table[0].tolist())
        hands_components = smpl_data["hands_components"]

        if rot_mode == "axisang":
            hands_mean = np.zeros(hands_components.shape[1]) if flat_hand_mean else smpl_data["hands_mean"]
            hands_mean = hands_mean.copy()
            hands_mean = torch.Tensor(hands_mean).unsqueeze(0)
            self.register_buffer("th_hands_mean", hands_mean)

        if rot_mode == "axisang" and use_pca == True:
            selected_components = hands_components[:ncomps]
            selected_components = torch.Tensor(selected_components)
            self.register_buffer("th_selected_comps", selected_components)

        # End

    def rotation_by_axisang(self, pose_coeffs):
        batch_size = pose_coeffs.shape[0]
        hand_pose_coeffs = pose_coeffs[:, self.rot_dim :]
        root_pose_coeffs = pose_coeffs[:, : self.rot_dim]
        if self.use_pca:
            full_hand_pose = hand_pose_coeffs.mm(self.th_selected_comps)
        else:
            full_hand_pose = hand_pose_coeffs

        # Concatenate back global rot
        full_poses = torch.cat([root_pose_coeffs, self.th_hands_mean + full_hand_pose], 1)

        pose_vec_reshaped = full_poses.contiguous().view(-1, 3)  # (B x N, 3)
        rot_mats = lietorch.SO3.exp(pose_vec_reshaped).matrix()[..., :3, :3]  # (B x N, 3, 3)
        full_rots = rot_mats.view(batch_size, 16, 3, 3)
        rotation_blob = {"full_rots": full_rots, "full_poses": full_poses}
        return rotation_blob

    def rotation_by_quaternion(self, pose_coeffs):
        batch_size = pose_coeffs.shape[0]
        full_quat_poses = pose_coeffs.view((batch_size, 16, 4))  # [B. 16, 4]
        full_rots = quaternion_to_rotation_matrix(full_quat_poses)  # [B, 16, 3, 3]
        full_poses = quaternion_to_angle_axis(full_quat_poses).reshape(batch_size, -1) # [B, 16 x 3]

        rotation_blob = {"full_rots": full_rots, "full_poses": full_poses}
        return rotation_blob

    def skinning_layer(self, full_rots, betas):
        batch_size = full_rots.shape[0]
        n_rot = int(full_rots.shape[1])  # 16

        root_rot = full_rots[:, 0, :, :]  # (B, 3, 3)
        hand_rot = full_rots[:, 1:, :, :]  # (B, 15, 3, 3)
        # Full axis angle representation with root joint

        # ============== Shape Blend Shape >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        # $ B_S = \sum_{n=1}^{|\arrow{\beta}|} \beta_n \mathbf{S}_n $  #Eq.4 in MANO
        B_S = torch.matmul(self.th_shapedirs, betas.transpose(1, 0)).permute(2, 0, 1)

        # $ \mathcal{J}(\bar{\mathbf{T}} + B_S)$ # Eq.10 in SMPL
        J = torch.matmul(self.th_J_regressor, (self.th_v_template + B_S))  # (B, 16, 3)

        # ============== Pose Blender Shape >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        flat_rot = torch.eye(3, dtype=full_rots.dtype, device=full_rots.device)  # (3, 3)
        flat_rot = flat_rot.view(1, 1, 3, 3).repeat(batch_size, hand_rot.shape[1], 1, 1)  # (B, 15, 3, 3)

        # $ R_n (\arrow{\theta}) -  R_n (\arrow{\theta}^{*}) $
        rot_minus_mean_flat = (hand_rot - flat_rot).reshape(batch_size, hand_rot.shape[1] * 9)  # (B, 15 x 9)

        # $ B_P = \sum_{n=1}^{9K} (R_n (\arrow{\theta}) -  R_n (\arrow{\theta}^{*})) * \mathbf{P}_n $  #Eq.3 in MANO
        B_P = torch.matmul(self.th_posedirs, rot_minus_mean_flat.transpose(0, 1)).permute(2, 0, 1)  # (B, 778, 3)

        # $ T_P =\bar{\mathbf{T}} + B_S + B_P $ # Eq.2 in MANO
        T_P = self.th_v_template + B_S + B_P

        # ============== Constructing $ G_{k} $ >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        # Global rigid transformation
        root_j = J[:, 0, :].contiguous().view(batch_size, 3, 1)
        root_transf = th_with_zeros(torch.cat([root_rot, root_j], 2))

        lev1_idxs = [1, 4, 7, 10, 13]
        lev2_idxs = [2, 5, 8, 11, 14]
        lev3_idxs = [3, 6, 9, 12, 15]
        lev1_rots = hand_rot[:, [idx - 1 for idx in lev1_idxs]]
        lev2_rots = hand_rot[:, [idx - 1 for idx in lev2_idxs]]
        lev3_rots = hand_rot[:, [idx - 1 for idx in lev3_idxs]]
        lev1_j = J[:, lev1_idxs]
        lev2_j = J[:, lev2_idxs]
        lev3_j = J[:, lev3_idxs]

        # From base to tips
        # Get lev1 results
        all_transforms = [root_transf.unsqueeze(1)]
        lev1_j_rel = lev1_j - root_j.transpose(1, 2)
        lev1_rel_transform_flt = th_with_zeros(torch.cat([lev1_rots, lev1_j_rel.unsqueeze(3)], 3).view(-1, 3, 4))
        root_trans_flt = root_transf.unsqueeze(1).repeat(1, 5, 1, 1).view(root_transf.shape[0] * 5, 4, 4)
        lev1_flt = torch.matmul(root_trans_flt, lev1_rel_transform_flt)
        all_transforms.append(lev1_flt.view(hand_rot.shape[0], 5, 4, 4))

        # Get lev2 results
        lev2_j_rel = lev2_j - lev1_j
        lev2_rel_transform_flt = th_with_zeros(torch.cat([lev2_rots, lev2_j_rel.unsqueeze(3)], 3).view(-1, 3, 4))
        lev2_flt = torch.matmul(lev1_flt, lev2_rel_transform_flt)
        all_transforms.append(lev2_flt.view(hand_rot.shape[0], 5, 4, 4))

        # Get lev3 results
        lev3_j_rel = lev3_j - lev2_j
        lev3_rel_transform_flt = th_with_zeros(torch.cat([lev3_rots, lev3_j_rel.unsqueeze(3)], 3).view(-1, 3, 4))
        lev3_flt = torch.matmul(lev2_flt, lev3_rel_transform_flt)
        all_transforms.append(lev3_flt.view(hand_rot.shape[0], 5, 4, 4))

        reorder_idxs = [0, 1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9, 14, 5, 10, 15]

        # Eq. 4 in SMPL
        G_k = torch.cat(all_transforms, 1)[:, reorder_idxs]
        th_transf_global = G_k

        # ============== Constructing $ G^{\prime}_{k} $ >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        joint_js = torch.cat([J, J.new_zeros(batch_size, 16, 1)], 2)
        tmp2 = torch.matmul(G_k, joint_js.unsqueeze(3))
        G_prime_k = (G_k - torch.cat([tmp2.new_zeros(*tmp2.shape[:2], 4, 3), tmp2], 3)).permute(0, 2, 3, 1)

        # ============== Finally, blender skinning >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        # we define $ T = w_{k, i} * G^{\prime}_k $
        T = torch.matmul(G_prime_k, self.th_weights.transpose(0, 1))  # (B, 4, 4, 778)

        T_P_homo = torch.cat(
            [T_P.transpose(2, 1), torch.ones((batch_size, 1, B_P.shape[1]), dtype=T.dtype, device=T.device)], dim=1
        )
        T_P_homo = T_P_homo.unsqueeze(1)  # (B, 1, 4, 778)

        # Eq. 7 in SMPL
        # Theorem: A \cdot B = (A * B^{T}).sum(1) # A is a matrix, B is a vector
        verts = (T * T_P_homo).sum(2).transpose(2, 1)  # (B, 778, 4)
        joints = th_transf_global[:, :, :3, 3]  # (B, 16, 3)
        verts = verts[:, :, :3]  # (B, 778, 3)
        # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

        # In addition to MANO reference joints we sample vertices on each finger
        # to serve as finger tips
        if self.side == "right":
            tips = verts[:, [745, 317, 444, 556, 673]]
        else:
            tips = verts[:, [745, 317, 445, 556, 673]]

        joints = torch.cat([joints, tips], 1)

        # ** original MANO joint order (right hand)
        #                16-15-14-13-\
        #                             \
        #          17 --3 --2 --1------0
        #        18 --6 --5 --4-------/
        #        19 -12 -11 --10-----/
        #          20 --9 --8 --7---/

        # Reorder joints to match SNAP definition
        joints = joints[:, [0, 13, 14, 15, 16, 1, 2, 3, 17, 4, 5, 6, 18, 10, 11, 12, 19, 7, 8, 9, 20]]

        if self.center_idx is not None:
            center_joint = joints[:, self.center_idx].unsqueeze(1)
        else:  # ! dummy center joint (B, 1, 3)
            center_joint = torch.zeros_like(joints[:, 0].unsqueeze(1))

        # apply center shift on verts and joints
        joints = joints - center_joint
        verts = verts - center_joint

        # apply center shift on global
        global_rot = th_transf_global[:, :, :3, :3]  # (B, 16, 3, 3)
        global_tsl = th_transf_global[:, :, :3, 3:]  # (B, 16, 3, 1)
        global_tsl = global_tsl - center_joint.unsqueeze(-1)  # (B, [16], 3, 1)
        global_transf = torch.cat([global_rot, global_tsl], dim=3)  # (B, 16, 3, 4)
        global_transf = th_with_zeros(global_transf.view(-1, 3, 4))
        global_transf = global_transf.view(batch_size, 16, 4, 4)

        skinning_blob = {
            "verts": verts,
            "joints": joints,
            "center_joint": center_joint,
            "transforms_abs": global_transf,
        }
        return skinning_blob

    def forward(self, pose_coeffs: torch.Tensor, betas: torch.Tensor, **kwargs):

        rot_blob = self.rotation_layer(pose_coeffs)
        full_rots = rot_blob["full_rots"]  # TENSOR
        skinning_blob = self.skinning_layer(full_rots, betas)
        output = MANOOutput(
            verts=skinning_blob["verts"],
            joints=skinning_blob["joints"],
            center_idx=self.center_idx,
            center_joint=skinning_blob["center_joint"],
            full_poses=rot_blob["full_poses"],
            betas=betas,
            transforms_abs=skinning_blob["transforms_abs"],
        )
        return output