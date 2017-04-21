from gunpowder import *
import math
import time
import random
import malis

random.seed(42)

affinity_neighborhood = malis.mknhood3d()
print(affinity_neighborhood)

# simulate many sources (here we point to the same file always)
sources = tuple(
    Hdf5Source(
            'test2.hdf',
            raw_dataset='volumes/raw',
            gt_dataset='volumes/labels/neuron_ids',
            gt_mask_dataset='volumes/labels/mask') +
    Normalize() +
    RandomLocation()
    for i in range(10)
)

# create a batch provider by concatenation of filters
batch_provider = (
        sources +
        RandomProvider() +
        ExcludeLabels([416759, 397008], 8) +
        Reject() +
        ElasticAugmentation([4,40,40], [0,2,2], [0,math.pi/2.0]) +
        SimpleAugment(transpose_only_xy=True) +
        IntensityAugment(0.9, 1.1, -0.1, 0.1, z_section_wise=True) +
        DefectAugment(prob_missing=0.1, prob_low_contrast=0.1, contrast_scale=0.1) +
        GrowBoundary(steps=3, only_xy=True) +
        AddGtAffinities(affinity_neighborhood) +
        # IntensityScaleShift(2, -1) +
        ZeroOutConstSections() +
        PreCache(
                lambda : BatchSpec(
                        (84,268,268),
                        (56,56,56),
                        with_gt=True,
                        with_gt_mask=True,
                        with_gt_affinities=True),
                cache_size=20,
                num_workers=10) +
        Snapshot(every=1) +
        PrintProfilingStats()
)

n = 20
print("Training for " + str(n) + " iterations")
batch_provider.initialize_all()
for i in range(n):
    batch_provider.request_batch(
            BatchSpec(
                        (84,268,268),
                        (56,56,56),
                        with_gt=True,
                        with_gt_mask=True,
                        with_gt_affinities=True))
