from typing import Optional, Tuple

import numpy as np
from sklearn.base import BaseEstimator

from blockmatrix import SpatioTemporalMatrix
from sklearn.preprocessing import StandardScaler


def shrinkage(
        X: np.ndarray,
        gamma: Optional[float] = None,
        T: Optional[np.ndarray] = None,
        S: Optional[np.ndarray] = None,
        block: bool = False,
        n_channels: int = 31,
        n_times: int = 5,
        standardize: bool = True,
) -> Tuple[np.ndarray, float]:
    # case for gamma = auto (ledoit-wolf)
    p, n = X.shape

    if standardize:
        sc = StandardScaler()  # standardize features
        X = sc.fit_transform(X.T).T
    Xn = X - np.repeat(np.mean(X, axis=1, keepdims=True), n, axis=1)
    if S is None:
        S = np.matmul(Xn, Xn.T)
    Xn2 = np.square(Xn)
    idxdiag = np.diag_indices(p)

    nu = np.mean(S[idxdiag])
    if T is None:
        T = nu * np.eye(p, p)
    if block and standardize:
        channel_vars = np.reshape(sc.scale_, (n_channels, n_times))
        block_vars = np.mean(channel_vars, axis=1)
        new_scale = np.tile(block_vars, n_times)
        sc.scale_ = new_scale

    # Ledoit Wolf
    V = 1.0 / (n - 1) * (np.matmul(Xn2, Xn2.T) - np.square(S) / n)
    if gamma is None:
        gamma = n * np.sum(V) / np.sum(np.square(S - T))
    if gamma > 1:
        print("logger.warning('forcing gamma to 1')")
        gamma = 1
    elif gamma < 0:
        print("logger.warning('forcing gamma to 0')")
        gamma = 0
    Cstar = (gamma * T + (1 - gamma) * S) / (n - 1)
    if standardize:  # scale back
        Cstar = sc.scale_[np.newaxis, :] * Cstar * sc.scale_[:, np.newaxis]

    return Cstar, gamma


class LearningFromLabelProportions(BaseEstimator):
    """
    Learning from label proportions classifier from Hübner et al 2017 [1]_.

    Parameters
    ----------
    ratio_matrix: np.ndarray of shape (4, 4)
        | ratio seq1/target   ratio seq1/non-target |
        | ratio seq2/target   ratio seq2/non-target |

    References
    ----------
    .. [1] Hübner, D., Verhoeven, T., Schmid, K., Müller, K. R., Tangermann, M., & Kindermans, P. J. (2017)
           Learning from label proportions in brain-computer interfaces: Online unsupervised learning with guarantees.
           PLOS ONE 12(4): e0175856.
           https://doi.org/10.1371/journal.pone.0175856
    """

    def __init__(
        self,
        ratio_matrix: np.ndarray = None,
        toeplitz_time=False,
        taper_time=None,
        toeplitz_spatial=False,
        taper_spatial=None,
        n_times=None,
        n_channels=31,
    ):
        if ratio_matrix is None:
            self.ratio_matrix = np.array([[3 / 8, 5 / 8], [2 / 18, 16 / 18]])
        self.pinv_ratio_matrix = np.linalg.inv(self.ratio_matrix)
        self.w = None
        self.b = None
        self.n_times = n_times
        self.n_channels = n_channels
        self.toeplitz_time = toeplitz_time
        self.taper_time = taper_time
        self.toeplitz_spatial = toeplitz_spatial
        self.taper_spatial = taper_spatial

        self.mu_T = None
        self.mu_NT = None

        self.stm_info = None

    def fit(self, X, y):
        """
        Parameters
        ----------
        X: np.ndarray
            Input data of shape (n_samples, n_chs, n_time)
        y: np.ndarray
            Sequence labels of X (not target/non-target labels),
             must be 1 (=sequence1) or 2 (=sequence2).
        """
        X1 = X[np.where(y == 1)]
        X2 = X[np.where(y == 2)]

        X = X.reshape(X.shape[0], -1)
        X1 = X1.reshape(X1.shape[0], -1)
        X2 = X2.reshape(X2.shape[0], -1)

        # Compute global covariance matrix
        C_cov, gamma = shrinkage(X.T)
        stm = SpatioTemporalMatrix(C_cov, n_chans=self.n_channels, n_times=self.n_times)

        if self.toeplitz_time:
            stm.force_toeplitz_offdiagonals()
        if self.taper_time is not None:
            stm.taper_offdiagonals(self.taper_time)
        stm.swap_primeness()
        if self.toeplitz_spatial:
            stm.force_toeplitz_offdiagonals(raise_spatial=False)
        if self.taper_spatial is not None:
            stm.taper_offdiagonals(self.taper_spatial)
        stm.swap_primeness()

        C_cov = stm.mat

        # Compute sequence-wise means
        average_O1 = np.mean(X1, axis=0)
        average_O2 = np.mean(X2, axis=0)

        mu_T = (
            self.pinv_ratio_matrix[0, 0] * average_O1 + self.pinv_ratio_matrix[0, 1] * average_O2
        )
        mu_NT = (
            self.pinv_ratio_matrix[1, 0] * average_O1 + self.pinv_ratio_matrix[1, 1] * average_O2
        )

        # use reconstructed means to compute w and b
        C_diff = mu_T - mu_NT
        C_mean = 0.5 * (mu_T + mu_NT)

        C_w = np.linalg.solve(C_cov, C_diff)
        C_w = 2 * C_w / np.dot(C_w.T, C_diff)
        C_b = np.dot(-C_w.T, C_mean)

        self.w = C_w
        self.b = C_b

        self.mu_T = mu_T
        self.mu_NT = mu_NT

        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        X = X.reshape(X.shape[0], -1)
        return np.dot(X, self.w) + self.b
