from functools import partial
import logging
logger = logging.getLogger(__name__)

import numpy

from lazyflow.graph import Operator, InputSlot, OutputSlot
from lazyflow.request import RequestLock, Request, RequestPool
from lazyflow.utility import OrderedSignal
from lazyflow.roi import getBlockBounds, getIntersectingBlocks, determineBlockShape

class OpFeatureMatrixCache(Operator):
    """
    - Request features and labels in blocks
    - For nonzero label pixels in each block, extract the label image
    - Cache the feature matrix for each block separately
    - Output the concatenation of all feature matrices
    """    
    FeatureImage = InputSlot()
    LabelImage = InputSlot()
    NonZeroLabelBlocks = InputSlot()  # TODO: Eliminate this slot. It isn't used...
    
    # Output is a single 'value', which is a 2D ndarray.
    # The first row is labels, the rest are the features.
    # (As a consequence of this, labels are converted to float)
    LabelAndFeatureMatrix = OutputSlot()
    
    ProgressSignal = OutputSlot() # For convenience of passing several progress signals 
                                  # to a downstream operator (such as OpConcatenateFeatureMatrices),  
                                  # we provide the progressSignal member as an output slot.

    # Aim for label request blocks of approximately 1 MB
    MAX_BLOCK_PIXELS = 1e6

    def __init__(self, *args, **kwargs):
        super(OpFeatureMatrixCache, self).__init__(*args, **kwargs)
        self._lock = RequestLock()
        
        self.progressSignal = OrderedSignal()
        self._progress_lock = RequestLock()
        
        self._blockshape = None
        self._blockwise_feature_matrices = {}
        self._dirty_blocks = set()
        self._block_locks = {}
    
    def setupOutputs(self):
        # We assume that channel the last axis
        assert self.FeatureImage.meta.getAxisKeys()[-1] == 'c'
        assert self.LabelImage.meta.getAxisKeys()[-1] == 'c'
        assert self.LabelImage.meta.shape[-1] == 1
        
        # For now, we assume that the two input images have the same shape (except channel)
        # This constraint could be relaxed in the future if necessary
        assert self.FeatureImage.meta.shape[:-1] == self.LabelImage.meta.shape[:-1],\
            "FeatureImage and LabelImage shapes do not match: {} vs {}"\
            "".format( self.FeatureImage.meta.shape, self.LabelImage.meta.shape )
    
        self.LabelAndFeatureMatrix.meta.shape = (1,)
        self.LabelAndFeatureMatrix.meta.dtype = object

        self.ProgressSignal.meta.shape = (1,)
        self.ProgressSignal.meta.dtype = object
        self.ProgressSignal.setValue( self.progressSignal )

        # Auto-choose a blockshape
        self._blockshape = determineBlockShape( self.LabelImage.meta.shape,
                                                OpFeatureMatrixCache.MAX_BLOCK_PIXELS )

    def execute(self, slot, subindex, roi, result):
        assert slot == self.LabelAndFeatureMatrix
        self.progressSignal(0.0)

        # Technically, this could result in strange progress reporting if execute() 
        #  is called by multiple threads in parallel.
        # This could be fixed with some fancier progress state, but 
        # (1) We don't expect that to by typical, and
        # (2) progress reporting is merely informational.
        num_dirty_blocks = len( self._dirty_blocks )
        def update_progress( result ):
            remaining_dirty = len( self._dirty_blocks )
            percent_complete = 95.0*(num_dirty_blocks - remaining_dirty)/num_dirty_blocks
            self.progressSignal( percent_complete )

        # Update all dirty blocks in the cache
        logger.debug( "Updating {} dirty blocks ({} are clean)"\
                      "".format(num_dirty_blocks, len(self._blockwise_feature_matrices)) )
        pool = RequestPool()
        for block_start in self._dirty_blocks:
            req = Request( partial(self._update_block, block_start ) )
            req.notify_finished( update_progress )
            pool.add( req )
        pool.wait()

        # Concatenate the all blockwise results
        if self._blockwise_feature_matrices:
            total_feature_matrix = numpy.concatenate( self._blockwise_feature_matrices.values(), axis=0 )
        else:
            total_feature_matrix = numpy.ndarray( shape=(0,0), dtype=numpy.float )

        self.progressSignal(100.0)
        result[0] = total_feature_matrix

    def propagateDirty(self, slot, subindex, roi):
        if slot == self.NonZeroLabelBlocks:
            # Label changes will be handled via labelimage dirtyness propagation
            return
        assert slot == self.FeatureImage or slot == self.LabelImage

        # Our blocks are tracked by label roi (1 channel)
        roi = roi.copy()
        roi.start[-1] = 0
        roi.stop[-1] = 1
        # Bookkeeping: Track the dirty blocks
        block_starts = getIntersectingBlocks( self._blockshape, (roi.start, roi.stop) )
        block_starts = map( tuple, block_starts )
        with self._lock:
            self._dirty_blocks.update( block_starts )

        # Output has no notion of roi. It's all dirty.
        self.LabelAndFeatureMatrix.setDirty()

    def _update_block(self, block_start):
        if block_start not in self._block_locks:
            with self._lock:
                if block_start not in self._block_locks:
                    self._block_locks[block_start] = RequestLock()
        with self._block_locks[block_start]:
            if block_start not in self._dirty_blocks:
                # Nothing to do if this block isn't actually dirty
                # (For parallel requests, its theoretically possible.)
                return
            block_roi = getBlockBounds( self.LabelImage.meta.shape, self._blockshape, block_start )
            # TODO: Shrink the requested roi using the nonzero blocks slot...
            #       ...or just get rid of the nonzero blocks slot...
            labels_and_features_matrix = self._extract_feature_matrix(block_roi)
            self._blockwise_feature_matrices[block_start] = labels_and_features_matrix
            with self._lock:
                self._dirty_blocks.remove(block_start)

    def _extract_feature_matrix(self, label_roi):
        num_feature_channels = self.FeatureImage.meta.shape[-1]
        labels = self.LabelImage(label_roi[0], label_roi[1]).wait()
        label_positions = numpy.nonzero(labels[...,0].view(numpy.ndarray))
        labels_matrix = labels[label_positions].astype(numpy.float).view(numpy.ndarray)
        
        if len(label_positions) == 0 or len(label_positions[0]) == 0:
            # No label points in this roi.  
            # Return an empty label&feature matrix (of the correct shape)
            return numpy.ndarray( shape=(0, 1 + num_feature_channels), dtype=numpy.float )

        # Shrink the roi to the bounding box of nonzero labels
        bounding_box_start = map( numpy.min, label_positions )
        bounding_box_stop = 1 + numpy.array( map( numpy.max, label_positions ) )

        # Since we're just requesting the bounding box, offset the feature positions by the box start
        feature_positions = numpy.transpose( numpy.transpose(label_positions) - numpy.array(bounding_box_start) )
        feature_positions = tuple(feature_positions)

        # Append channel roi (all feature channels)
        feature_roi_start = list(bounding_box_start) + [0]
        feature_roi_stop = list(bounding_box_stop) + [num_feature_channels]

        # Request features
        features = self.FeatureImage(feature_roi_start, feature_roi_stop).wait()

        # Cast as plain ndarray (not VigraArray), since we don't need/want axistags
        features_matrix = features[feature_positions].view(numpy.ndarray)
        return numpy.concatenate( (labels_matrix, features_matrix), axis=1)

        