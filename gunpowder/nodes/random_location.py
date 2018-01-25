import copy
import logging
from random import randint

import numpy as np
from skimage.transform import integral_image, integrate
from gunpowder.batch_request import BatchRequest
from gunpowder.coordinate import Coordinate
from gunpowder.roi import Roi
from .batch_filter import BatchFilter

logger = logging.getLogger(__name__)

class RandomLocation(BatchFilter):
    '''Choses a batch at a random location in the bounding box of the upstream
    provider.

    The random location is chosen such that the batch request roi lies entirely
    inside the provider's roi.

    If `min_masked` and `mask` are set, only batches are returned that have at
    least the given ratio of masked-in voxels. This is in general faster than
    using the ``Reject`` node, at the expense of storing an integral array of
    the complete mask.

    If 'ensure_nonempty' is set to a :class:``PointsKey``, only batches are
    returned that have at least one point of this point collection within the
    requested ROI.

    Args:

        min_masked(float, optional): If non-zero, require that the random
            sample contains at least that ratio of masked-in voxels.

        mask(:class:``ArrayKey``): The array to use for mask checks.

        ensure_nonempty(:class:``PointsKey``, optional): Ensures that when
            finding a random location, a request for ``ensure_nonempty`` will
            contain at least one point. This does only work if all upstream
            nodes are deterministic (e.g., there is no
            :class:``RandomProvider`` upstream).
    '''

    def __init__(self, min_masked=0, mask=None, ensure_nonempty=None):

        self.min_masked = min_masked
        self.mask = mask
        self.mask_spec = None
        self.ensure_nonempty = ensure_nonempty


    def setup(self):

        self.upstream_spec = self.get_upstream_provider().spec
        self.upstream_roi = self.upstream_spec.get_total_roi()

        if self.upstream_roi is None:
            raise RuntimeError("Can not draw random samples from a provider that does not have a bounding box.")

        if self.mask and self.min_masked > 0:

            assert self.mask in self.upstream_spec, "Upstream provider does not have %s"%self.mask
            self.mask_spec = self.upstream_spec.array_specs[self.mask]

            logger.info("requesting complete mask...")

            mask_request = BatchRequest({self.mask: self.mask_spec})
            mask_batch = self.get_upstream_provider().request_batch(mask_request)

            logger.info("allocating mask integral array...")

            mask_data = mask_batch.arrays[self.mask].data
            mask_integral_dtype = np.uint64
            logger.debug("mask size is " + str(mask_data.size))
            if mask_data.size < 2**32:
                mask_integral_dtype = np.uint32
            if mask_data.size < 2**16:
                mask_integral_dtype = np.uint16
            logger.debug("chose %s as integral array dtype"%mask_integral_dtype)

            self.mask_integral = np.array(mask_data>0, dtype=mask_integral_dtype)
            self.mask_integral = integral_image(self.mask_integral)

        # clear bounding boxes of all provided arrays and points -- 
        # RandomLocation does not have limits (offsets are ignored)
        for identifier, spec in self.spec.items():
            spec.roi = None
            self.updates(identifier, spec)

    def prepare(self, request):

        shift_roi = None

        logger.debug("request: %s", request.array_specs)
        logger.debug("my spec: %s", self.spec)

        if request.array_specs.keys():
            lcm_voxel_size = self.spec.get_lcm_voxel_size(
                    request.array_specs.keys())
            logger.debug(
                "restricting random locations to multiples of voxel size %s",
                lcm_voxel_size)
        else:
            lcm_voxel_size = None


        for identifier, spec in request.items():
            request_roi = spec.roi
            if identifier in self.upstream_spec:
                provided_roi = self.upstream_spec[identifier].roi
            else:
                raise Exception(
                    "Requested %s, but upstream does not provide "
                    "it."%identifier)
            type_shift_roi = provided_roi.shift(-request_roi.get_begin()).grow((0,0,0),-request_roi.get_shape())

            if shift_roi is None:
                shift_roi = type_shift_roi
            else:
                shift_roi = shift_roi.intersect(type_shift_roi)

        logger.debug("valid shifts for request in " + str(shift_roi))

        assert shift_roi is not None and shift_roi.size() > 0, (
                "Can not satisfy batch request, no location covers all "
                "requested ROIs.")

        if lcm_voxel_size is not None:
            lcm_shift_roi = shift_roi/lcm_voxel_size
        else:
            lcm_shift_roi = shift_roi

        good_location_found_for_mask, good_location_found_for_points = False, False
        while not good_location_found_for_mask or not good_location_found_for_points:
            # select a random point inside ROI
            random_shift = Coordinate(
                    randint(int(begin), int(end-1))
                    for begin, end in zip(lcm_shift_roi.get_begin(), lcm_shift_roi.get_end()))
            if lcm_voxel_size is not None:
                random_shift *= lcm_voxel_size
            initial_random_shift = copy.deepcopy(random_shift)
            logger.debug("random shift: " + str(random_shift))

            good_location_found_for_mask, good_location_found_for_points = False, False
            if self.ensure_nonempty is not None:

                focused_points_roi = request.points_spec[self.ensure_nonempty].roi
                focused_points_offset = focused_points_roi.get_offset()
                focused_points_shape  = focused_points_roi.get_shape()

                # prefetch points in roi of ensure_nonempty
                request_for_focused_points = BatchRequest()
                request_for_focused_points.points_spec[self.ensure_nonempty] = PointsSpec(roi=focused_points_roi.shift(random_shift))
                batch_of_points    = self.get_upstream_provider().request_batch(request_for_focused_points)
                point_ids_in_batch = batch_of_points.points[self.ensure_nonempty].data.keys()

                if len(point_ids_in_batch) > 0:
                    chosen_point_id       = np.random.choice(point_ids_in_batch, size=1)[0]
                    chosen_point_location = Coordinate(batch_of_points.points[self.ensure_nonempty].data[chosen_point_id].location)
                    distance_focused_roi_to_chosen_point = chosen_point_location - (initial_random_shift + focused_points_offset)
                    local_shift_roi = Roi(offset=(distance_focused_roi_to_chosen_point - (focused_points_shape - Coordinate((2,2,2)))),
                                          shape=(focused_points_shape-Coordinate((2,2,2))))

                    # set max trials to prevent endless loop and search for suitable local shift in impossible cases
                    trial_nr, max_trials, good_local_shift = 0, 100000, False
                    while not good_local_shift:
                        trial_nr += 1
                        local_shift = Coordinate(
                                    randint(int(begin), int(end))
                                    for begin, end in zip(local_shift_roi.get_begin(), local_shift_roi.get_end()))
                        # make sure that new shift matches ROIs of all requested arrays
                        if shift_roi.contains(initial_random_shift + local_shift):
                            random_shift = initial_random_shift + local_shift
                            assert Roi(offset=random_shift + focused_points_offset,
                                        shape=focused_points_shape).contains(chosen_point_location)
                            good_local_shift, good_location_found_for_points = True, True

                        if trial_nr == max_trials:
                            good_location_found_for_points = False
                            break

                else:
                    good_location_found_for_points = False

            else:
                good_location_found_for_points = True

            if self.mask and self.min_masked > 0:
                # get randomly chosen mask ROI
                request_mask_roi = request.array_specs[self.mask].roi
                request_mask_roi = request_mask_roi.shift(random_shift)

                # get coordinates inside mask array
                mask_voxel_size = self.spec[self.mask].voxel_size
                request_mask_roi_in_array = request_mask_roi/mask_voxel_size
                request_mask_roi_in_array -= self.mask_spec.roi.get_offset()/mask_voxel_size

                # get number of masked-in voxels
                num_masked_in = integrate(
                    self.mask_integral,
                    [request_mask_roi_in_array.get_begin()],
                    [request_mask_roi_in_array.get_end()-(1,)*self.mask_integral.ndim]
                )[0]

                mask_ratio = float(num_masked_in)/request_mask_roi_in_array.size()
                logger.debug("mask ratio is %f", mask_ratio)

                if mask_ratio >= self.min_masked:
                    logger.debug("good batch found")
                    good_location_found_for_mask = True
                else:
                    logger.debug("bad batch found")

            else:
                good_location_found_for_mask = True

        # shift request ROIs
        self.random_shift = random_shift
        for specs_type in [request.array_specs, request.points_specs]:
            for (key, spec) in specs_type.items():
                roi = spec.roi.shift(random_shift)
                logger.debug("new %s ROI: %s"%(key, roi))
                specs_type[key].roi = roi
                assert self.upstream_roi.contains(roi)


    def process(self, batch, request):
        # reset ROIs to request
        for (array_key, spec) in request.array_specs.items():
            batch.arrays[array_key].spec.roi = spec.roi
        for (points_key, spec) in request.points_specs.items():
            batch.points[points_key].spec.roi = spec.roi

        # change shift point locations to lie within roi
        for points_key in request.points_specs.keys():
            for point_id, point in batch.points[points_key].data.items():
                batch.points[points_key].data[point_id].location -= self.random_shift
