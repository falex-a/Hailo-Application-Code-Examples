import numpy as np
import tensorflow as tf
import logging

from inspectors.base_inspector import BaseInspector

from hailo_sdk_client.exposed_definitions import InferenceContext


class ClippingInspector(BaseInspector):
    def _run(self):
        if self._dataset is None:
            self._logger.warning(f"Skipping {self.name}, dataset was not provided")
            return
        hist_data, hist_ranges = self._collect_hist_per_layer()
        self.check_histograms(hist_data, hist_ranges)

    def _collect_hist_per_layer(self):
        qparams = self._runner.get_params_translated()
        with self._runner.infer_context(InferenceContext.SDK_FP_OPTIMIZED) as ctx:
            model = self._runner.get_keras_model(ctx)._model
            hist_layers = [lname for lname, layer in model.layers.items() if layer.activation_atomic_op]
            model.compile(save_interlayer=hist_layers)

            # TODO: Find correct ranges or collect them based on the dataset...
            hist_ranges = {}
            for lname in hist_layers:
                l_min, l_max = qparams[lname]['limvals_out:0']
                hist_ranges[lname] = np.array([l_min, l_max])
            full_result = {lname: np.zeros(100, dtype=np.uint32) for lname in hist_layers}

            @tf.function
            def infer_hist(data):
                model(data)
                batch_result = {lname: np.zeros(100, dtype=np.uint32) for lname in hist_layers}
                for layer, tensor in model.interlayer_tensors.items():
                    if model.layers[layer].num_outputs == 1:
                        hist1 = tf.histogram_fixed_width(tensor, hist_ranges[layer])
                        batch_result[layer] += hist1
                    else:
                        for ten in tensor:
                            hist1 = tf.histogram_fixed_width(ten, hist_ranges[layer])
                            batch_result[layer] += hist1
                return batch_result

            for data_sample, _ in self._dataset.batch(1):
                batch_result = infer_hist(data_sample)
                for lname, lresult in batch_result.items():
                    full_result[lname] += lresult.numpy().astype('uint32')
        return full_result, hist_ranges

    def check_histograms(self, hist_data, hist_ranges):
        any_clip_rec = False
        THRESHOLD = 3
        for layer, hist in hist_data.items():
            bin_size = (hist_ranges[layer][1] - hist_ranges[layer][0]) / len(hist)
            right_msg = left_msg = ""
            min_bins = np.where(np.cumsum(hist) <= THRESHOLD)[0]
            bin1 = 0 if len(min_bins) == 0 else np.min(min_bins)
            bin2 = np.max(np.where(np.cumsum(hist[::-1])[::-1] > THRESHOLD)[0]) + 1
            count_left = np.sum(hist[:bin1])
            count_right = np.sum(hist[bin2:])
            log_level = 0
            if bin1 != 0 and (hist_ranges[layer][0] + bin_size * (bin1)) < 0:
                left_msg = f"{bin1}% of the range (of the low range) has {count_left} items"
                new_log_level = logging.DEBUG if bin1 <= 5 else logging.WARNING
                log_level = max(log_level, new_log_level)
            if bin2 != len(hist) and (hist_ranges[layer][0] + bin_size * (bin2)) > 0:
                right_msg = f"{len(hist) - bin2}% of the range (of the high range) has {count_right} items"
                new_log_level = logging.DEBUG if bin2 >= 95 else logging.WARNING
                log_level = max(log_level, new_log_level)
            should_right = len(right_msg) > 0
            should_left = len(left_msg) > 0
            if should_right or should_left:
                any_clip_rec = True
                spacer = ', ' if should_left and should_right else ''
                max_range = hist_ranges[layer][0] + bin_size * (bin2) if should_right else hist_ranges[layer][1]
                min_range = hist_ranges[layer][0] + bin_size * (bin1) if should_left else hist_ranges[layer][0]
                message = f"Layer {layer}, {left_msg}{spacer}{right_msg}. Suggested manual range [{min_range:.03f}, {max_range:.03f}]"
                self._logger.log(log_level, message)
        if any_clip_rec:
            self._logger.info(f"Items threshold is {THRESHOLD}, Histogram has {len(hist)} bins. "
                              f"Warning is printed if more than 5% of the range has only 3 items. "
                              f"Consider analyzing the data in depth before applying clipping")
            self._logger.info("In some cases the range might not be fixable and affected by other factors.")
            self._logger.info("In general, activation clipping suggestion if very sensitive to the calibration set. "
                              "Applying activation clipping in some cases might reduce accuracy.")

    # TODO: filter by snr?
