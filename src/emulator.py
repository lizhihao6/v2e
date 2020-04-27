"""
DVS simulator.
Compute events from input frames.

@author: Zhe He
@contact: zhehe@student.ethz.ch
@credits: Yuhuang Hu
@latest updaste: 2019-Jun-13
"""

import os
import cv2
import numpy as np
import logging
import h5py
from engineering_notation import EngNumber  # only from pip
from src.v2e_utils import all_images, read_image, video_writer, checkAddSuffix
from src.output.aedat2_output import AEDat2Output
from src.output.ae_text_output import DVSTextOutput
# import rosbag # not yet for python 3

logger = logging.getLogger(__name__)


def lin_log(x, threshold=20):
    """
    linear mapping + logrithmic mapping.
    @author: Zhe He
    @contact: hezhehz@live.cn
    """

    # converting x into np.float32.
    if x.dtype is not np.float32:
        x = x.astype(np.float32)
    f = (1 / (threshold)) * np.log(threshold)
    y = np.piecewise(
        x,
        [x < threshold, x >= threshold],
        [lambda x: x * f,
         lambda x: np.log(x)]
    )

    return y


class EventEmulator(object):
    """compute events based on the input frame.
    - author: Zhe He
    - contact: zhehe@student.ethz.ch
    """

    # todo add refractory period

    def __init__(
            self,
            pos_thres=0.21,
            neg_thres=0.17,
            sigma_thres=0.03,
            cutoff_hz=0,
            leak_rate_hz=0.1,
            refractory_period_s=0,
            seed=42,
            output_folder:str=None,
            dvs_h5:str=None,
            dvs_aedat2:str=None,
            dvs_text:str=None,
            rotate180:bool=False,
            show_input:str=None # 'logBaseFrame','lpLogFrame', 'diff_frame'
            # dvs_rosbag=None
    ):
        """
        Parameters
        ----------
        base_frame: np.ndarray
            [height, width]. If None, then it is initialized from first data
        pos_thres: float, default 0.21
            nominal threshold of triggering positive event in log intensity.
        neg_thres: float, default 0.17
            nominal threshold of triggering negative event in log intensity.
        sigma_thres: float, default 0.03
            std deviation of threshold in log intensity.
            cutoff_hz: float,
            3dB cutoff frequency in Hz of DVS photoreceptor
            leak_rate_hz: float
            leak event rate per pixel in Hz, from junction leakage in reset switch
        seed: int, default=0
            seed for random threshold variations, fix it to nonzero value to get same mismatch every time
        dvs_aedat2, dvs_h5, dvs_text: str
            names of output data files or None

        """

        logger.info("ON/OFF log_e temporal contrast thresholds: {} / {} +/- {}".format(pos_thres, neg_thres, sigma_thres))
        self.baseLogFrame = None
        self.sigma_thres = sigma_thres
        self.pos_thres = pos_thres # initialized to scalar, later overwritten by random value array
        self.neg_thres = neg_thres  # initialized to scalar, later overwritten by random value array
        self.pos_thres_nominal = pos_thres
        self.neg_thres_nominal = neg_thres
        self.cutoff_hz=cutoff_hz
        self.leak_rate_hz=leak_rate_hz
        self.refractory_period_s=refractory_period_s
        self.output_width = None
        self.output_height = None  # set on first frame
        self.rotate180=rotate180
        self.show_input=show_input
        np.random.seed(seed)

        # if leak_rate_hz>0:
        #     logger.warning('leak events not yet implemented; leak_rate_hz={} will be ignored'.format(leak_rate_hz))
        if refractory_period_s>0:
            logger.warning('refractory period not yet implemented; refractory_period_s={} will be ignored'.format(refractory_period_s))

        self.output_folder=output_folder
        self.dvs_h5=dvs_h5
        self.dvs_aedat2=dvs_aedat2
        self.dvs_text=dvs_text
        self.num_events_total=0
        self.num_events_on=0
        self.num_events_off=0

        if self.output_folder:
            if dvs_h5:
                path=os.path.join(self.output_folder, dvs_h5)
                path=checkAddSuffix(path,'.h5')
                logger.info('opening event output dataset file ' + path)
                self.dvs_h5 = h5py.File(path, "w")
                self.dvs_h5_dataset = self.dvs_h5.create_dataset(
                    name="event",
                    shape=(0, 4),
                    maxshape=(None, 4),
                    dtype="uint32")
            if dvs_aedat2:
                path=os.path.join(self.output_folder,dvs_aedat2)
                path=checkAddSuffix(path,'.aedat')
                logger.info('opening AEDAT-2.0 output file '+path)
                self.dvs_aedat2=AEDat2Output(path,rotate180)
            if dvs_text:
                path=checkAddSuffix(path,'.txt')
                path=os.path.join(self.output_folder,dvs_text)
                logger.info('opening text DVS output file '+path)
                self.dvs_text=DVSTextOutput(path)

    def _init(self, firstFrameLinear):
        logger.debug('initializing random temporal contrast thresholds from from base frame')
        self.baseLogFrame = lin_log(firstFrameLinear)  # base_frame are memorized lin_log pixel values
        self.lpLogFrame = np.copy(self.baseLogFrame)
        # take the variance of threshold into account.
        self.pos_thres = np.random.normal(self.pos_thres, self.sigma_thres, firstFrameLinear.shape)
        # to avoid the situation where the threshold is too small.
        self.pos_thres[self.pos_thres < 0.01] = 0.01
        self.neg_thres = np.random.normal(self.neg_thres, self.sigma_thres, firstFrameLinear.shape)
        self.neg_thres[self.neg_thres < 0.01] = 0.01

    def reset(self):
        '''resets so that next use will reinitialize the base frame
        '''
        self.num_events_total = 0
        self.num_events_on = 0
        self.num_events_off = 0
        self.baseLogFrame = None
        self.lasttime=None
        self.lpLogFrame=None

    def _show(self,inp:np.ndarray):
        min=np.min(inp)
        img=((inp-min)/(np.max(inp)-min))
        if self.rotate180: img=np.rot90(img,k=2)
        cv2.imshow(__name__,img)

    def accumulate_events(self, new_frame: np.ndarray, t_start: float, t_end: float) -> np.ndarray:
        """Compute events in new frame.

        Parameters
        ----------
        new_frame: np.ndarray
            [height, width]
        t_start: float
            starting timestamp of new frame in float seconds
        t_end: float
            ending timestamp of new frame in float seconds

        Returns
        -------
        events: np.ndarray if any events, else None
            [N, 4], each row contains [timestamp, y cordinate,
            x cordinate, sign of event]. # TODO validate that this order of x and y is correctly documented
        """

        if t_start > t_end:
            raise ValueError("t_start must be smaller than t_end")

        # todo handle K frames, not just 1

        # base_frame: the change detector input, stores memorized brightness values
        # new_frame: the new intensity frame input
        # log_frame: the lowpass filtered brightness values
        if self.baseLogFrame is None:
            self._init(new_frame)
            self.lasttime = t_start
            return None
        # apply log transform and lowpass filter here
        deltaTime = t_start - self.lasttime
        if self.cutoff_hz<=0:
            eps=1
        else:
            tau=1/(np.pi*2*self.cutoff_hz)
            eps= deltaTime / tau
            if eps>1: eps=1
        logNewFrame = lin_log(new_frame)
        self.lpLogFrame= (1 - eps) * self.lpLogFrame + eps * logNewFrame
        self.lasttime=t_start

        # switch in diff change amp leaks at some rate equivalent to some hz of ON events
        # actual leak rate depends on threshold for each pixel
        # we want nominal rate leak_rate_Hz, so
        if self.leak_rate_hz>0:
            deltaLeak=deltaTime*self.leak_rate_hz/self.pos_thres_nominal
            self.baseLogFrame-=deltaLeak # subract so it increases ON events

        diff_frame =  self.lpLogFrame - self.baseLogFrame  # log intensity (brightness) change from memorized values

        if self.show_input:
            if self.show_input=='baseLogFrame':
                self._show(self.baseLogFrame)
            elif self.show_input=='lpLogFrame':
                self._show(self.lpLogFrame)
            elif self.show_input=='diff_frame':
                self._show(diff_frame)
            else:
                logger.error("don't know about showing {}".format(self.show_input))
        pos_frame = np.zeros_like(diff_frame)  # initialize
        neg_frame = np.zeros_like(diff_frame)
        poxIdxs = diff_frame > 0
        pos_frame[poxIdxs] = diff_frame[poxIdxs]  # pixels with ON changes
        negIdxs = diff_frame < 0
        neg_frame[negIdxs] = np.abs(diff_frame[negIdxs])

        pos_evts_frame = pos_frame // self.pos_thres  # compute quantized numbers of ON events for each pixel
        pos_iters = int(pos_evts_frame.max())  # compute number of times to pass over array to compute separated ON events
        neg_evts_frame = neg_frame // self.neg_thres  # same for OFF events
        neg_iters = int(neg_evts_frame.max())

        pos_evts_frame.argmax()
        num_iters = max(pos_iters, neg_iters)  # need to iterative this many times

        events = []

        for i in range(num_iters):

            # intermediate timestamps are linearly spaced
            # they start after the t_start to make sure that there is space from previous frame
            # they end at t_end
            # e.g. t_start=0, t_end=1, num_iters=2, i=0,1
            # ts=1*1/2, 2*1/2
            ts = t_start + (t_end - t_start) * (i + 1) / (num_iters)

            # for each iteration, compute the ON and OFF event locations for that threshold amount of change
            pos_cord = (pos_frame > self.pos_thres * (i + 1))
            neg_cord = (neg_frame > self.neg_thres * (i + 1))

            # generate events
            pos_event_xy = np.where(pos_cord)
            num_pos_events = pos_event_xy[0].shape[0]
            neg_event_xy = np.where(neg_cord)
            num_neg_events = neg_event_xy[0].shape[0]
            num_events = num_pos_events + num_neg_events

            self.num_events_off+=num_neg_events
            self.num_events_on+=num_pos_events
            self.num_events_total+=num_events

            # sort out the positive event and negative event
            if num_pos_events > 0:
                pos_events = np.hstack(
                    (np.ones((num_pos_events, 1), dtype=np.float32) * ts,
                     pos_event_xy[1][..., np.newaxis],
                     pos_event_xy[0][..., np.newaxis],
                     np.ones((num_pos_events, 1), dtype=np.float32) * 1))

            else:
                pos_events = None

            if num_neg_events > 0:
                neg_events = np.hstack(
                    (np.ones((num_neg_events, 1), dtype=np.float32) * ts,
                     neg_event_xy[1][..., np.newaxis],
                     neg_event_xy[0][..., np.newaxis],
                     np.ones((num_neg_events, 1), dtype=np.float32) * -1))

            else:
                neg_events = None

            if pos_events is not None and neg_events is not None:
                events_tmp = np.vstack((pos_events, neg_events))
            else:
                if pos_events is not None:
                    events_tmp = pos_events
                else:
                    events_tmp = neg_events
            # randomly order events to prevent bias to one corner
            if events_tmp is not None:
                events_tmp = events_tmp.take(np.random.permutation(events_tmp.shape[0]), axis=0)

            if i == 0: # update the base frame only once, after we know how many events per pixel
                # add to memorized brightness values just the events we emitted.
                # don't add the remainder. the next aps frame might have sufficient value
                # to trigger another event or it might not,
                # but we are correct in not storing the current frame brightness
                if num_pos_events > 0:
                    self.baseLogFrame[pos_cord] += \
                        pos_evts_frame[pos_cord] * self.pos_thres[pos_cord]
                if num_neg_events > 0:
                    self.baseLogFrame[neg_cord] -= \
                        neg_evts_frame[neg_cord] * self.neg_thres[neg_cord]  # neg_thres is >0

            if num_events > 0:
                events.append(events_tmp)

        if len(events) > 0:
            events = np.vstack(events)
            if self.dvs_h5 is not None: # todo add h5 output
                pass
                # # convert data to uint32 (microsecs) format
                # tmp_events[:, 0] = tmp_events[:, 0] * 1e6
                # tmp_events[tmp_events[:, 3] == -1, 3] = 0
                # tmp_events = tmp_events.astype(np.uint32)
                #
                # # save events
                # self.dvs_h5_dataset.resize(
                #     event_dataset.shape[0] + tmp_events.shape[0],
                #     axis=0)
                #
                # event_dataset[-tmp_events.shape[0]:] = tmp_events
                # self.dvs_h5.flush()
            if self.dvs_aedat2 is not None:
                self.dvs_aedat2.appendEvents(events)
            if self.dvs_text is not None:
                self.dvs_text.appendEvents(events)

        if len(events) > 0:
            return events
        else:
            return None

#############################################################################################################

class EventFrameRenderer(object):
    """ Deprecated
    class for rendering event frames.
    - author: Zhe He
    - contact: zhehe@student.ethz.ch
    """

    def __init__(self,
                 data_path,
                 output_path,
                 input_fps,
                 output_fps,
                 pos_thres,
                 neg_thres,
                 preview=None):
        """
        Parameters
        ----------
        data_path: str
            path of frames.
        output_path: str
            path of output video.
        input_fps: int
            frame rate of input video.
        output_fps: int
            frame rate of output video.
        """

        self.data_path = data_path
        self.output_path = output_path
        self.input_fps = input_fps
        self.output_fps = output_fps
        self.pos_thres = pos_thres
        self.neg_thres = neg_thres
        self.preview=preview
        self.preview_resized=False
    def _get_events(self):
        """Get all events.
        """
        images = all_images(self.data_path)
        num_frames = len(images)
        input_ts = np.linspace(
            0,
            num_frames / self.input_fps,
            num_frames,
            dtype=np.float)
        base_frame = read_image(images[0])
        logger.info('base frame shape: {}'.format(base_frame.shape))
        height = base_frame.shape[0]
        width = base_frame.shape[1]
        emulator = EventEmulator(
            pos_thres=self.pos_thres,
            neg_thres=self.neg_thres
        )

        event_list = list()
        time_list = list()
        pos_list = list()

        # index of the first element at timestamp t.
        pos = 0

        for idx in range(1, num_frames):
            new_frame = read_image(images[idx])
            t_start = input_ts[idx - 1]
            t_end = input_ts[idx]
            tmp_events = emulator.accumulate_events(
                new_frame,
                t_start,
                t_end
            )

            if tmp_events is not None:
                event_list.append(tmp_events)
                pos_list.append(pos)
                time_list.append(t_end)

                # update pos
                pos += tmp_events.shape[0]

            if (idx + 1) % 20 == 0:
                logger.info("Image2Events processed {} frames".format(EngNumber(idx + 1)))

        event_arr = np.vstack(event_list)
        logger.info("generated {} events".format(EngNumber(event_arr.shape[0])))

        return event_arr, time_list, pos_list, num_frames, height, width

    def render(self):
        """Render event frames."""
        (event_arr, time_list, pos_list,
         num_frames, height, width) = self._get_events()

        output_ts = np.linspace(
            0,
            num_frames / self.input_fps,
            int(num_frames / self.input_fps * self.output_fps),
            dtype=np.float)
        clip_value = 2
        histrange = [(0, v) for v in (height, width)]
        out = video_writer(os.path.join(self.output_path, 'output.avi'), width=width, height=height)
        for ts_idx in range(output_ts.shape[0] - 1):
            # assume time_list is sorted.
            start = np.searchsorted(time_list,
                                    output_ts[ts_idx],
                                    side='right')
            end = np.searchsorted(time_list,
                                  output_ts[ts_idx + 1],
                                  side='right')
            # select events, assume that pos_list is sorted
            if end < len(pos_list):
                events = event_arr[pos_list[start]: pos_list[end], :]
            else:
                events = event_arr[pos_list[start]:, :]

            pol_on = (events[:, 3] == 1)
            pol_off = np.logical_not(pol_on)
            img_on, _, _ = np.histogram2d(
                events[pol_on, 2], events[pol_on, 1],
                bins=(height, width), range=histrange)
            img_off, _, _ = np.histogram2d(
                events[pol_off, 2], events[pol_off, 1],
                bins=(height, width), range=histrange)
            if clip_value is not None:
                integrated_img = np.clip(
                    (img_on - img_off), -clip_value, clip_value)
            else:
                integrated_img = (img_on - img_off)
            img = (integrated_img + clip_value) / float(clip_value * 2)
            out.write(cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR))
            if self.preview:
                cv2.namedWindow(__name__, cv2.WINDOW_NORMAL)
                if self.rotate:
                    np.rot90(img, k=2)
                cv2.imshow(__name__, img)
                if not self.preview_resized:
                    cv2.resizeWindow(__name__, 800, 600)
                    self.preview_resized = True
                cv2.waitKey(30)  # 30 hz playback
            if ts_idx % 20 == 0:
                logger.info('Rendered {} frames'.format(ts_idx))
            # if cv2.waitKey(int(1000 / 30)) & 0xFF == ord('q'):
            #     break
        out.release()
