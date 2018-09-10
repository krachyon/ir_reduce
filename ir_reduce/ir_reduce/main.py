"""
Module for reducing infrared Data, currently for NOTCam but intended for a future instrument
"""
import numpy as np
import astropy
import os
import itertools
from astropy import units as u
from astropy.nddata import CCDData
import astropy.io.fits as fits
import ccdproc
from astropy.stats import SigmaClip
from astropy.io import fits
from .image_discovery import ImageGroup
from .run_sextractor_scamp import run_astroref as run_scamp

from functools import reduce

from typing import List, Tuple, Iterable, Set, Union, Any, Dict
from numpy import s_  # numpy helper to create slices by indexing this

# Globals: Fits-columns #todo: put in module
filter_column = 'NCFLTNM2'  # TODO NOTCam-specific
filter_vals = ('H', 'J', 'Ks')

image_category = 'IMAGECAT'
object_ID = 'OBJECT'


def read_and_sort(bads: Iterable[str], flats: Iterable[str], exposures: Iterable[str]) -> Dict[str, ImageGroup]:
    """
    read in images and sort them by filter, return the CCDDatas

    :param bads: list of paths to bad pixel frames
    :param flats: list of paths to flat frames
    :param exposures: list of paths to images
    :return: A dictionary which maps filter-id -> [bads, flats, images]
    """
    # TODO move asserts into unit-test or introduce a validation flag/wrapper
    assert (all(os.path.isfile(path) for path in itertools.chain(bads, flats, exposures)))

    image_datas = [astropy.nddata.CCDData.read(image) for image in exposures]
    flat_datas = [astropy.nddata.CCDData.read(image) for image in flats]
    bad_datas = [astropy.nddata.CCDData.read(image) for image in bads]

    ret = dict()
    # for all filter present in science data we need at least a flatImage and a bad pixel image
    for filter_id in filter_vals:
        try:
            images_with_filter = [image for image in image_datas if image.header[filter_column] == filter_id]
            # only science images allowed
            assert (all((image.header[image_category] == 'SCIENCE' for image in images_with_filter)))

            flats_with_filter = [image for image in flat_datas if image.header[filter_column] == filter_id]
            # assert (len(flats_with_filter) == 1)  # TODO only one flat
            # TODO this assumes that you pass all possible flats. But CLI only wants one flat right now
        except KeyError as err:
            print("looks like there's no filter column in the fits data")
            raise err

        # bad pixel maps are valid, no matter the filter
        ret[filter_id] = ImageGroup(bad_datas, flats_with_filter, images_with_filter)

    return ret


def standard_process(bads: List[CCDData], flat: CCDData, images: List[CCDData]) -> List[CCDData]:
    """
    Do the ccdproc operation on a list of images. includes some extra logic for NOTCAM images to get the
    gain and readnoise out of the headers
    :param bads:
    :param flat:
    :param images:
    :return:
    """
    bad = reduce(lambda x, y: x.astype(bool) | y.astype(bool), (i.data for i in bads))  # combine bad pixel masks

    reduceds = []
    for image in images:
        image.mask = bad
        # TODO that's from the quicklook-package, probably would want to do this individually for every sensor area
        gain = (image.header['GAIN1'] + image.header['GAIN2'] + image.header['GAIN3'] + image.header['GAIN4']) / 4
        readnoise = (image.header['RDNOISE1'] + image.header['RDNOISE2'] + image.header['RDNOISE3'] + image.header[
            'RDNOISE4']) / 4
        reduced = ccdproc.ccd_process(image,
                                      oscan=None,
                                      error=True,
                                      gain=gain * u.electron / u.count,
                                      # TODO check if this is right or counts->adu required
                                      readnoise=readnoise * u.electron,
                                      dark_frame=None,
                                      master_flat=flat,
                                      bad_pixel_mask=bad)
        reduceds.append(reduced)
    return reduceds


def tiled_process(bads: List[CCDData], flat: CCDData, images: List[CCDData]) -> List[CCDData]:
    """
    Do the ccdproc operation on a list of images. includes some extra logic for NOTCAM images to get the
    gain and readnoise out of the headers
    :param bads:
    :param flat:
    :param images:
    :return:
    """
    bad = reduce(lambda x, y: x.astype(bool) | y.astype(bool), (i.data for i in bads))  # combine bad pixel masks

    reduceds = []
    for image in images:
        image.mask = bad
        # TODO this is really sketchy as it does 4x the work
        # Tiling: http://www.not.iac.es/instruments/notcam/guide/observe.html#reductions
        # 0-> LL, 1->LR, 2->UR, 3->UL
        tile_table = [None, s_[512:, 0:512], s_[512:, 512:], s_[0:512, 512:], s_[0:512, 0:512]]
        reduced = image.copy()

        for idx in range(1, 5):
            gain = image.header['GAIN' + str(idx)]
            readnoise = image.header['RDNOISE' + str(idx)]
            tile = ccdproc.ccd_process(image,
                                       oscan=None,
                                       error=True,
                                       gain=gain * u.electron / u.count,
                                       # TODO check if this is right or counts->adu required
                                       readnoise=readnoise * u.electron,
                                       dark_frame=None,
                                       master_flat=flat,
                                       bad_pixel_mask=bad)
            image.data[tile_table[idx]] = tile.data[tile_table[idx]]
            reduced.header = tile.header
            reduced.wcs = tile.wcs
            reduced.unit = tile.unit
        reduceds.append(reduced)
    return reduceds


def skyscale(image_list: Iterable[CCDData], method: str = 'subtract',
             cut: Tuple[Union[slice, int]] = s_[200:800, 200:800]) -> List[CCDData]:
    """
    Subtract/divide out the median sky value of some images
    :param image_list: The images to process
    :param method: either 'subtract' or 'divide'
    :param cut: what region of the images to consider to create the median sky value
    :return: images, with sky removed
    """
    sigma_clip = SigmaClip(sigma=3., iters=3)
    filtered_data = [sigma_clip(image.data) for image in image_list]

    medians = np.array([np.median(data[cut]) for data in filtered_data])
    #airmass = sum((image.header['AIRMASS'] for image in image_list))  # TODO needed?

    # TODO from original code:
    # Calculate scaling relatively to last image median
    # Why not average or median-median?
    if method == 'subtract':
        medians = (medians - medians[-1])
        ret = [CCDData.subtract(image, median * u.electron) for image, median in zip(image_list, medians)]
    elif method == 'divide':
        medians = medians / medians[-1]
        ret = [CCDData.subtract(image, median * u.electron) for image, median in zip(image_list, medians)]
    else:
        raise ValueError('method needs to be either subtract or divide')

    # TODO: write/return sky file?
    return ret


def fix_pix(img: CCDData) -> CCDData:
    im = img.data
    mask = img.mask
    import scipy.ndimage as ndimage
    """
    taken from https://www.iaa.csic.es/~jmiguel/PANIC/PAPI/html/_modules/reduce/calBPM.html#fixPix 
    (GPLv3)
    
    Applies a bad-pixel mask to the input image (im), creating an image with
    masked values replaced with a bi-linear interpolation from nearby pixels.
    Probably only good for isolated badpixels.

    Usage:
      fixed = fixpix(im, mask, [iraf=])

    Inputs:
      im = the image array
      mask = an array that is True (or >0) where im contains bad pixels
      iraf = True use IRAF.fixpix; False use numpy and a loop over
             all pixels (extremelly low)

    Outputs:
      fixed = the corrected image

    v1.0.0 Michael S. Kelley, UCF, Jan 2008

    v1.1.0 Added the option to use IRAF's fixpix.  MSK, UMD, 25 Apr
           2011

    Notes
    -----
    - Non-IRAF algorithm is extremelly slow.
    """

    # create domains around masked pixels
    dilated = ndimage.binary_dilation(mask)
    domains, n = ndimage.label(dilated)

    # loop through each domain, replace bad pixels with the average
    # from nearest neigboors
    y, x = np.indices(im.shape, dtype=np.int)[-2:]
    # x = xarray(im.shape)
    # y = yarray(im.shape)
    cleaned = im.copy()
    for d in (np.arange(n) + 1):
        # find the current domain
        i = (domains == d)

        # extract the sub-image
        x0, x1 = x[i].min(), x[i].max() + 1
        y0, y1 = y[i].min(), y[i].max() + 1
        subim = im[y0: y1, x0: x1]
        submask = mask[y0: y1, x0: x1]
        subgood = (submask == False)

        cleaned[i * mask] = subim[subgood].mean()

    img.data = cleaned
    return img


def interpolate(img: CCDData):
    """
    Takes a image with a mask for bad pixels and interpolates over the bad pixels

    :param img: the image you want to interpolate bad pixels in
    :param dofixpix: use the fixpix-algorithm?
    :return: interpolated image

    """
    # TODO combiner does not care about this and marks it invalid still
    from astropy.convolution import CustomKernel
    from astropy.convolution import interpolate_replace_nans

    # TODO this here doesn't really work all that well -> extended regions cause artifacts at border
    kernel_array = np.array([[1, 1, 1], [1, 1, 1], [1, 1, 1]]) / 9  # average of all surrounding pixels
    # noinspection PyTypeChecker
    kernel = CustomKernel(
        kernel_array)  # TODO the original pipeline used fixpix, which says it uses linear interpolation

    img.data[np.logical_not(img.mask)] = np.NaN
    img.data = interpolate_replace_nans(img.data, kernel)

    return img


def do_everything(bads: Iterable[str],
                  flats: Iterable[str],
                  images: Iterable[str],
                  output: Union[str, bool],
                  filter_letter: str = 'J',  # TODO: allow 'all'
                  combine: str = 'median',
                  skyscale_method: str = 'subtract') -> Tuple[CCDData, str, bytes]:
    """
    Take a list of files for badPixel, flatfield and exposures + a bunch of processing parameters and reduce them
    to write an output filec
    :param bads: list of paths to bad pixel frames
    :param flats: list of paths to flat frames
    :param images: list of paths to images
    :param output: Path to write output image to. No output if false-y
    :param filter_letter: which spectral band to look at
    :param combine: either 'median' or 'average'
    :param skyscale_method: either 'subtract' or 'divide'
    :return: (combined_output, scamp_output, sextractor_output)
    """
    # TODO the images this spits out are not quite

    assert (filter_letter in filter_vals)
    read_files = read_and_sort(bads, flats, images)[filter_letter]

    # TODO distortion correct here

    # Perform basic reduction operations
    processed = standard_process(read_files.bad, read_files.flat[0], read_files.images)
    skyscaled = skyscale(processed, skyscale_method)
    fixed = [fix_pix(image) for image in skyscaled]

    # Reproject everything to the world-coordinate system of the first image
    wcs = fixed[0].wcs
    reprojected = [ccdproc.wcs_project(img, wcs) for img in fixed]

    # TODO option to align with cross correlation (see image_registration)

    # overlay images
    combiner = ccdproc.Combiner(reprojected)
    output_image = combiner.median_combine() if combine == 'median' else combiner.average_combine()
    # WTF. ccdproc.Combiner is not giving back good metadata.
    # need to set WCS manually agai and convert header to astropy.fits.io.Header object from an ordered dict
    # not replacing this can cause weird errors during file writing/wcs conversion
    output_image.wcs = wcs
    output_image.header = astropy.io.fits.header.Header(output_image.header)

    # The output has 3 hdus: image and error/mask. This confuses scamp, so only take the image to feed it to scamp
    first_hdu = output_image.to_hdu()[0]
    scamp_input = CCDData(first_hdu.data, header=first_hdu.header, unit=first_hdu.header['bunit'])
    scamp_data, sextractor_data = run_scamp(scamp_input)

    # PV?_? (distortion) entries are not handled well by wcslib and by extension astropy.
    # just Remove them as a workaround
    scamp_header = fits.Header.fromstring(scamp_data, sep='\n')
    for entry in scamp_header.copy():
        if entry.startswith('PV'):
            scamp_header.pop(entry)

    output_image.header.update(scamp_header)
    output_image.wcs = astropy.wcs.WCS(scamp_header)

    if output:
        with open(output + 'scamp.head', 'w') as f:
            f.write(scamp_data)
        with open(output + 'sextractor.fits', 'wb') as f:
            f.write(sextractor_data)
        try:
            output_image.write(output, overwrite='True')
        except OSError as err:
            print(err, "writing output failed")

    return output_image, scamp_data, sextractor_data