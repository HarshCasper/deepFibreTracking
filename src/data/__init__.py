"""
The data module is handling all kinds of DWI-data.

Use this as a starting point to represent your loaded DWI-scan.
This module provides methods helping you to implement datasets, 
environments and all other kinds of modules with the requirement
to work directly with the data.  
"""

import os
import warnings
from types import SimpleNamespace

import torch

from dipy.core.gradients import gradient_table
from dipy.io import read_bvals_bvecs
from dipy.denoise.localpca import localpca
from dipy.denoise.pca_noise_estimate import pca_noise_estimate
from dipy.align.reslice import reslice
from dipy.segment.mask import median_otsu
from scipy.interpolate import RegularGridInterpolator

from dipy.tracking.streamline import interpolate_vector_3d, interpolate_scalar_3d
import dipy.reconst.dti as dti

import numpy as np
import nibabel as nb
from nibabel.affines import apply_affine

from src.config import Config
import src.data.exceptions 

class RawData(SimpleNamespace):
    """
    This class represents the raw loaded data, providing attributes to access it.
    
    You should mainly see it as part of an DataContainer, which provides helpful methods
    to manipulate it or access (interpolated, processed) values

    Attributes
    ----------
    bvals: ndarray
        the B-values of the image
    bvecs: ndarray
        the B-vectors matching the bvals
    img: nibabel.nifti1.Nifti1Image
        the DWI-Image
    t1: ndarray
        the T1-File data
    gtab: dipy.core.gradients.GradientTable
        the calculated gradient table
    dwi: ndarray
        the raw DWI data
    aff: ndarray
        The affine used for coordinate transformation
    binarymask: ndarray
        A binarymask usable to separate brain from the rest
    b0: ndarray
        The b0 image usable for normalization etc.
    """

class MovableData():
    """
    This class can be used to make classes handling multiple tensors more easily movable.

    With simple inheritance, all of those must be instances of `torch.Tensor` or `MovableData`.
    Also, they have to be direct attributes of the object and are not allowed to be nested.

    Attributes
    ----------
    device: torch.device, optional
        The device the movable data currently is located on.

    Inheritance
    -----------
    To modify and inherit the `MovableData` class, overwrite the following functions:

    `_get_tensors()`
        This should return all `torch.Tensor` and `MovableData` instances of your class,
        in a key value pair `dict`.

    `_set_tensor(key, tensor)`
        This should replace the reference to the tensor with given key with the new, moved tensor.

    If those two methods are properly inherited, the visible functions should work as normal.
    If you plan on add other class types to the `_get_tensors` method, make sure that they implement
    the cuda, cpu, to and get_device methods in the same manner as `torch.Tensor` instances.
    """
    def __init__(self, device=None):
        """
        Parameters
        ----------
        device : torch.device, optional
            The device which the `MovableData` should be moved to on load, by default cpu.
        """
        if device is None:
            device = torch.device("cpu")
        self.device = device

    def _get_tensors(self):
        """
        Returns a dict containing all `torch.Tensor` and `MovableData` instances
        and their assigned keys.

        The default implementation searches for those on the attribute level.
        If your child class contains tensors at other positions, it is recommendable to
        overwrite this function and the `_set_tensor` function.

        Returns
        -------
        dict
            The dict containing every `torch.Tensor` and `MovableData` with their assigned keys.

        See Also
        --------
        _set_tensor: implementations depend on each other
        """
        tensors = {}
        for key, value in vars(self).items():
            if isinstance(value, torch.Tensor) or isinstance(value, MovableData):
                tensors[key] = value
        return tensors

    def _set_tensor(self, key, tensor):
        """
        Sets the tensor with the assigned key to his value.

        In the default implementation, this works analogously to `_get_tensors`:
        It sets the attribute with the name key to the given object/tensor.
        If your child class contains tensors at other positions, it is recommendable to
        overwrite this function and the `_get_tensors` function.

        Parameters
        ----------
        key : str
            The key of the original tensor.
        tensor : object
            The new tensor which should replace the original one.

        See Also
        --------
        _get_tensors: implementations depend on each other
        """
        setattr(self, key, tensor)

    def cuda(self, device=None, non_blocking=False, memory_format=torch.preserve_format):
        """
        Returns this object in CUDA memory.

        If this object is already in CUDA memory and on the correct device,
        then no movement is performed and the original object is returned.

        Parameters
        ----------
        device : `torch.device`, optional
            The destination GPU device. Defaults to the current CUDA device.
        non_blocking : `bool`, optional
             If `True` and the source is in pinned memory, the copy will be asynchronous with
             respect to the host. Otherwise, the argument has no effect, by default `False`.
        memory_format : `torch.memory_format`, optional
            the desired memory format of returned Tensor, by default `torch.preserve_format`.

        Returns
        -------
        MovableData
            The object moved to specified device
        """
        for attribute, tensor in self._get_tensors().items():
            cuda_tensor = tensor.cuda(device=device, non_blocking=non_blocking,
                                      memory_format=memory_format)
            self._set_tensor(attribute, cuda_tensor)
            self.device = cuda_tensor.device
        return self

    def cpu(self, memory_format=torch.preserve_format):
        """
        Returns a copy of this object in CPU memory.

        If this object is already in CPU memory and on the correct device,
        then no copy is performed and the original object is returned.

        Parameters
        ----------
        memory_format : `torch.memory_format`, optional
            the desired memory format of returned Tensor, by default `torch.preserve_format`.

        Returns
        -------
        MovableData
            The object moved to specified device
        """
        for attribute, tensor in self._get_tensors().items():
            cpu_tensor = tensor.cpu(memory_format=memory_format)
            self._set_tensor(attribute, cpu_tensor)
        self.device = torch.device('cpu')
        return self

    def to(self, *args, **kwargs):
        """
        Performs Tensor dtype and/or device conversion.
        A `torch.dtype` and `torch.device` are inferred from the arguments of
        `self.to(*args, **kwargs)`.

        Here are the ways to call `to`:

        `to(dtype, non_blocking=False, copy=False, memory_format=torch.preserve_format)` -> Tensor
            Returns MovableData with specified `dtype`

        `to(device=None, dtype=None, non_blocking=False, copy=False,
        memory_format=torch.preserve_format)` -> Tensor
            Returns MovableData on specified `device`

        `to(other, non_blocking=False, copy=False)` -> Tensor
            Returns MovableData with same `dtype` and `device` as `other`
        Returns
        -------
        MovableData
            The object moved to specified device
        """
        for attribute, tensor in self._get_tensors().items():
            tensor = tensor.to(*args, **kwargs)
            self._set_tensor(attribute, tensor)
            self.device = tensor.device
        return self

    def get_device(self):
        """
        For CUDA tensors, this function returns the device ordinal of the GPU on which the tensor
        resides. For CPU tensors, an error is thrown.

        Returns
        -------
        int
            The device ordinal

        Raises
        ------
        DeviceNotRetrievableError
            This description is thrown if the tensor is currently on the cpu,
            therefore, no device ordinal exists.
        """
        if self.device.type == "cpu":
            raise DeviceNotRetrievableError(self.device)
        return self.device.index

class DataContainer():
    """
    The DataContainer class is representing a single DWI Dataset.

    It contains basic functions to work with the data.
    The data itself is accessable in the `self.data` attribute,
    which is of the type `RawData`

    Attributes
    ----------
    options: SimpleNamespace
        The configuration of the current DWI.
    path: str
        The path of the loaded DWI-Data.
    data: RawData
        The dwi data, referenced in the RawData's attributes.
    id: str
        An identifier of the current DWI-Data including its preprocessing.

    Inheritance
    -----------
    To inherit the `DataContainer` class, you are advised to use the following function:

    `_retrieve_data(self, file_names, denoise=False, b0_threshold=None)`
        This reads the properties of the given path based on the filenames and denoises the image, if applicable.
        Then it returns a RawData object.

    which is automatically called in the constructor.
    For correct inheritance, call the constructor with the correct filenames and
    pass denoise and threshold values. Example for HCP:

    >>> paths = {'bvals':'bvals', 'bvecs':'bvecs', 'img':'data.nii.gz',
                 't1':'T1w_acpc_dc_restore_1.25.nii.gz', 'mask':'nodif_brain_mask.nii.gz'}
    >>> DataContainer.__init__(self, path, paths, denoise=denoise, b0_threshold=b0_threshold)

    Then, your data is automatically correctly loaded and the other functions are working as well.
    """

    def __init__(self, path, file_names, denoise=None, b0_threshold=None):
        """
        Parameters
        ----------
        path : str
            The path leading to the DWI-Data Folder.
        file_names : dict
            A dictionary containg the file names for the specific values, e.g. 'img':'data.nii.gz'.
        denoise : bool, optional
            A boolean indicating wether the given data should be denoised,
            by default as in configuration file.
        b0_threshold : float, optional
            A single value indicating the b0 threshold used for b0 calculation,
            by default as in configuration file.
        """
        if denoise is None:
            denoise = Config.get_config().getboolean("data", "denoise", fallback="no")
        if b0_threshold is None:
            b0_threshold = Config.get_config().getfloat("data", "b0-threshold", fallback="10")
        self.options = RawData()
        self.options.denoised = denoise
        self.options.cropped = False
        self.options.normalized = False
        self.options.b0_threshold = b0_threshold
        self.path = path.rstrip(os.path.sep)
        self.data = self._retrieve_data(file_names, denoise=denoise, b0_threshold=b0_threshold)
        self.id = ("DataContainer" + self.path.replace(os.path.sep, "-") + "-"
                   "b0thr-" + str(b0_threshold))
        if self.options.denoised:
            self.id = self.id + "-denoised"


        x_range = np.arange(self.data.dwi.shape[0])
        y_range = np.arange(self.data.dwi.shape[1])
        z_range = np.arange(self.data.dwi.shape[2])
        self.interpolator = RegularGridInterpolator((x_range,y_range,z_range), self.data.dwi)

    def _retrieve_data(self, file_names, denoise=False, b0_threshold=10):
        """
        Reads data from specific files and returns them as object.

        This functions reads the filenames of the DWI image and loads/parses them accordingly.
        Also, it denoises them, if specified and generates a b0 image.

        The `file_names` param should be a dict with the following keys:
        `['bvals', 'bvecs', 'img', 't1', 'mask']`

        Parameters
        ----------
        file_names : dict
            The filenames, or relative paths from `self.path`.
        denoise : bool, optional
            A boolean indicating wether the given data should be denoised, by default False
        b0_threshold : float, optional
            A single value indicating the b0 threshold used for b0 calculation, by default 10.0

        Returns
        -------
        RawData
            An object holding all data as attributes, usable for further processing.

        Raises
        ------
        DataContainerNotLoadableError
            This error is thrown if one or multiple files cannot be found.
        """
        data = RawData()
        try:
            data.bvals, data.bvecs = read_bvals_bvecs(os.path.join(self.path, file_names['bvals']),
                                                      os.path.join(self.path, file_names['bvecs']))
            data.img = nb.load(os.path.join(self.path, file_names['img']))
            data.t1 = nb.load(os.path.join(self.path, file_names['t1'])).get_data()
        except FileNotFoundError as error:
            raise DataContainerNotLoadableError(self.path, error.filename) from None

        data.gtab = gradient_table(bvals=data.bvals, bvecs=data.bvecs)
        data.dwi = data.img.get_data().astype("float32")
        data.aff = data.img.affine
        data.fa = None

        if denoise:
            sigma = pca_noise_estimate(data.dwi, data.gtab, correct_bias=True,
                                       smooth=Config.get_config().getint("denoise", "smooth",
                                                                         fallback="3"))
            data.dwi = localpca(data.dwi, sigma=sigma,
                                patch_radius=Config.get_config().getint("denoise", "pathRadius",
                                                                        fallback="2"))
        if 'mask' in file_names:
            data.binarymask = nb.load(os.path.join(self.path, file_names['mask'])).get_data()
        else:
            _, data.binarymask = median_otsu(data.dwi[..., 0], 2, 1)

        data.b0 = data.dwi[..., data.bvals < b0_threshold].mean(axis=-1)

        return data

    def to_ijk(self, points):
        """
        Converts given RAS+ points to IJK in DataContainers Image Coordinates.

        The conversion happens using the affine of the DWI image.
        It should be noted that the dimension of the given point array stays the same.

        Parameters
        ----------
        points : ndarray
            The points to convert.

        Returns
        -------
        ndarray
            The converted points.

        See also
        --------
        to_ras(points): the reverse method.
        """
        aff = np.linalg.inv(self.data.aff)
        return apply_affine(aff, points)

    def to_ras(self, points):
        """
        Converts given IJK points in DataContainers Coordinate System to RAS+.

        The conversion happens using the affine of the DWI image.
        It should be noted that the dimension of the given point array stays the same.

        Parameters
        ----------
        points : ndarray
            The points to convert.

        Returns
        -------
        ndarray
            The converted points.

        See also
        --------
        to_ijk(points): the reverse method.
        """
        aff = self.data.aff
        return apply_affine(aff, points)

    def get_interpolated_dwi(self, points, postprocessing=None):
        """
        Returns interpolated dwi for given RAS+ points.

        The shape of the input points will be retained for the return array,
        only the last dimension will be changed from 3 to the (interpolated) DWI-size accordingly.
        
        If you provide a postprocessing method, the interpolated data is then fed through this postprocessing option.

        Parameters
        ----------
        points : ndarray
            The array containing the points. Shape is matched in output.
        postprocessing : data.Postprocessing, optional
            A postprocessing method, e.g res100, raw, spherical_harmonics etc.
            which will be applied to the output.

        Returns
        -------
        ndarray
            The DWI-Values interpolated for the given points.
            The input shape is matched aside of the last dimension.
        """

        points = self.to_ijk(points)
        shape = points.shape
        new_shape = (*shape[:-1], self.data.dwi.shape[-1])

        result = self.interpolator(points.reshape(-1, 3))
        result = result.reshape(new_shape)

        if postprocessing is not None:
            result = postprocessing(result, self.data.b0, 
                                 self.data.bvecs, 
                                 self.data.bvals)
        return result

    def crop(self, b_value=None, max_deviation=None, ignore_already_cropped=False):
        """Crops the dataset based on B-value.

        This function crops the DWI-Image based on B-Value.
        Pay attention to the fact that every value deviating more than `max_deviation` from the specified `b_value`
        will be irretrievably removed in the object.

        Parameters
        ----------
        b_value : float, optional
            The b-value used for cropping, by default as in configuration.
        max_deviation : float, optional
            The maximum deviation allowed around given b-value, by default as in configuration.
        ignore_already_cropped : bool, optional
            If set to true, no `DWIAlreadyCroppedError` will be thrown even if applicable,
            by default `False`

        Returns
        -------
        DataContainer
            self after applying the crop.

        Raises
        ------
        DWIAlreadyCroppedError
            This error will be thrown to prevent multiple cropping which
            - because of the irretrievably - could lead to unexpected results.
            To use multiple cropping intentionally, set ignore_already_cropped to True.
        """
        if self.options.cropped and not ignore_already_cropped:
            raise DWIAlreadyCroppedError(self, self.options.crop_b, self.options.crop_max_deviation)

        if b_value is None:
            b_value = Config.get_config().getfloat("data", "cropB-Value", fallback="1000.0")
        if max_deviation is None:
            max_deviation = Config.get_config().getfloat("data", "cropMaxDeviation", fallback="100")

        indices = np.where(np.abs(self.data.bvals - b_value) < max_deviation)[0]

        self.data.dwi = self.data.dwi[..., indices]
        self.data.bvals = self.data.bvals[indices]
        self.data.bvecs = self.data.bvecs[indices]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.data.gtab = gradient_table(bvals=self.data.bvals, bvecs=self.data.bvecs)

        self.options.cropped = True
        self.options.crop_b = b_value
        self.options.crop_max_deviation = max_deviation
        self.id = self.id + "-cropped[{b}, {dev}]".format(b=b_value, dev=max_deviation)

        x_range = np.arange(self.data.dwi.shape[0])
        y_range = np.arange(self.data.dwi.shape[1])
        z_range = np.arange(self.data.dwi.shape[2])
        self.interpolator = RegularGridInterpolator((x_range,y_range,z_range), self.data.dwi)
        return self

    def normalize(self):
        """Normalize DWI Data based on b0 image.

        The weights are divided by their b0 value.

        Raises
        ------
        DWIAlreadyCroppedError
            If the DWI is already cropped, normalization doesn't make much sense anymore. 
            Thus this is prevented.

        Returns
        -------
        DataContainer
            self after applying the normalization.
        """
        if self.options.cropped:
            raise DWIAlreadyCroppedError(self, self.options.crop_b, self.options.crop_max_deviation)

        if self.options.normalized:
            raise DWIAlreadyNormalizedError(self)

        b0 = self.data.b0[..., None]

        nb_erroneous_voxels = np.sum(self.data.dwi > b0)
        if nb_erroneous_voxels != 0:
            weights = np.minimum(self.data.dwi, b0)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            weights = weights / b0
            weights[np.logical_not(np.isfinite(weights))] = 0.
            # TODO check if that warnings catching can be prevented

        self.data.dwi = weights
        self.id = self.id + "-normalized"
        self.options.normalized = True

        x_range = np.arange(self.data.dwi.shape[0])
        y_range = np.arange(self.data.dwi.shape[1])
        z_range = np.arange(self.data.dwi.shape[2])
        self.interpolator = RegularGridInterpolator((x_range,y_range,z_range), self.data.dwi)
        return self

    def generate_fa(self):
        """Generates the FA Values for DataContainer.

        Normalization is required. 
        It is recommended to call the routine ahead of cropping,
        such that the FA values make sense, but it is not prohibited

        Returns
        -------
        ndarray
            Fractional anisotropy (FA) calculated from cached eigenvalues.
        """
        if self.options.cropped:
            warnings.warn("""You are generating the fa values from already cropped DWI. 
            You typically want to generate_fa() before you crop the data.""")
        dti_model = dti.TensorModel(self.data.gtab, fit_method='LS')
        dti_fit = dti_model.fit(self.data.dwi)
        self.data.fa = dti_fit.fa
        return self.data.fa
    def get_fa(self):
        """Retrieves the previously generated FA values

        Returns
        -------
        ndarray
            Fractional anisotropy (FA) calculated from cached eigenvalues.
        
        See Also
        --------
        generate_fa: The method generating the fa values which are returned here.
        """
        return self.data.fa

class HCPDataContainer(DataContainer):
    """
    The HCPDataContainer class is representing a single HCP Dataset.

    It contains basic functions to work with the data.
    The data itself is accessable in the `self.data` attribute.

    The `self.data` attribute contains the following
        - bvals: the B-values
        - bvecs: the B-vectors matching the bvals
        - img: the DWI-Image file
        - t1: the T1-File data
        - gtab: the calculated gradient table
        - dwi: the real DWI data
        - aff: the affine used for coordinate transformation
        - binarymask: a binarymask usable to separate brain from the rest
        - b0: the b0 image usable for normalization etc.

    Attributes
    ----------
    options: SimpleNamespace
        The configuration of the current DWI Data.
    path: str
        The path of the loaded DWI-Data.
    data: RawData
        The dwi data, referenced in the RawData's attributes.
    id: str
        An identifier of the current DWI-data including its preprocessing.
    hcp_id: int
        The HCP ID from which the data was retrieved.
    """

    def __init__(self, hcpid, denoise=None, b0_threshold=None):
        """
        Parameters
        ----------
        hcpid : int
            The id of the HCP Dataset to load.
        denoise : bool, optional
            A boolean indicating wether the given data should be denoised,
            by default as in configuration file.
        b0_threshold : float, optional
            A single value indicating the b0 threshold used for b0 calculation,
            by default as in configuration file.
        """
        path = Config.get_config().get("data", "pathHCP", fallback='data/HCP/{id}').format(id=hcpid)
        self.hcp_id = hcpid
        paths = {'bvals':'bvals', 'bvecs':'bvecs', 'img':'data.nii.gz',
                 't1':'T1w_acpc_dc_restore_1.25.nii.gz', 'mask':'nodif_brain_mask.nii.gz'}
        DataContainer.__init__(self, path, paths, denoise=denoise, b0_threshold=b0_threshold)
        self.id = ("HCPDataContainer-HCP{id}-b0thr-{b0}"
                   .format(id=self.hcp_id, b0=self.options.b0_threshold))
        if self.options.denoised:
            self.id = self.id + "-denoised"

class ISMRMDataContainer(DataContainer):
    """
    The ISMRMDataContainer class is representing the artificial generated ISMRM Data.

    It contains basic functions to work with the data.
    The data itself is accessable in the `self.data` attribute.

    The `self.data` attribute contains the following
        - bvals: the B-values
        - bvecs: the B-vectors matching the bvals
        - img: the DWI-Image file
        - t1: the T1-File data
        - gtab: the calculated gradient table
        - dwi: the real DWI data
        - aff: the affine used for coordinate transformation
        - binarymask: a binarymask usable to separate brain from the rest
        - b0: the b0 image usable for normalization etc.

    Attributes
    ----------
    options: SimpleNamespace
        The configuration of the current DWI.
    path: str
        The path of the loaded DWI-Data.
    data: RawData
        The dwi data, referenced in the RawData's attributes.
    id: str
        An identifier of the current DWI-data including its preprocessing.

    See Also
    --------
    src.tracker.ISMRMReferenceStreamlinesTracker: The streamlines matching this dataset.
    """
    def __init__(self, denoise=None, b0_threshold=None):
        """
        Parameters
        ----------
        denoise : bool, optional
            A boolean indicating wether the given data should be denoised,
            by default as in configuration file.
        b0_threshold : float, optional
            A single value indicating the b0 threshold used for b0 calculation,
            by default as in configuration file.
        """
        path = Config.get_config().get("data", "pathISMRM", fallback='data/ISMRM2015')
        paths = {'bvals':'Diffusion.bvals', 'bvecs':'Diffusion.bvecs',
                 'img':'Diffusion.nii.gz', 't1':'T1.nii.gz'}
        DataContainer.__init__(self, path, paths, denoise=denoise, b0_threshold=b0_threshold)

        self.id = "ISMRMDataContainer-b0thr-{b0}".format(b0=self.options.b0_threshold)
        if self.options.denoised:
            self.id = self.id + "-denoised"
