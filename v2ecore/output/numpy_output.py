import numpy as np
import logging
from engineering_notation import EngNumber  # only from pip
import atexit

logger = logging.getLogger(__name__)

class DVSNumpyOutput:
    '''
    return [h, w, steps]
    '''

    def __init__(self, filepath: str, height: int, width: int, diff: float, max_steps:int):
        self.filepath = filepath
        self.height, self.width = height, width
        self.diff = diff
        self.max_steps = max_steps
        # edit below to match your device from https://inivation.com/support/software/fileformat/#aedat-20
        self.numEventsWritten = 0
        logging.info('opening text DVS output file {}'.format(filepath))
        # maybe use larger data type when diff is huge
        self.events = np.zeros([height, width, max_steps]).astype(np.int8)
        atexit.register(self.cleanup)
        self.flipx=False # set both flipx and flipy to rotate TODO replace with rotate180
        self.flipy=False

    def cleanup(self):
        self.close()

    def close(self):
        if self.events is None:
            return
        if self.events[:, :, 0].max()==self.events[:, :, 0].min()==0:
            self.events[:, :, 0] = self.events[:, :, 1]
        logger.info("Closing {} after writing {} events".format(self.filepath, EngNumber(self.numEventsWritten)))
        if "s3://" in self.filepath:
            from aiisp_tool.utils.oss_helper import OSSHelper
            helper = OSSHelper()
            helper.upload(self.events, self.filepath, "numpy")
        else:
            np.save(self.filepath, self.events)
        self.events = None
            

    def appendEvents(self, events: np.ndarray):
        if self.events is None:
            raise Exception('output file closed already')

        if len(events) == 0:
            return
        n = events.shape[0]
        t = (events[:, 0]).astype(np.float)
        x = events[:, 1].astype(np.int32)
        if self.flipx: x = (self.width - 1) - x  # 0 goes to sizex-1
        y = events[:, 2].astype(np.int32)
        if self.flipy: y = (self.height - 1) - y
        p = (events[:, 3]).astype(np.int32) # -1 / 1
        step = np.minimum(t//self.diff, self.max_steps-1).astype(np.uint8)
        self.events[y, x, step] += p
        # for i in range(n):
            # step = int(t[i] // self.diff)
            # if step >= self.max_steps:
                # continue
            # self.events[y[i], x[i], step] += p[i]

        self.numEventsWritten += n

# class DVSTextOutputTest: # test from src.output.ae_text_output import DVSTextOutputTest
#     f = DVSTextOutput('aedat-text-test.txt')
#     e = [[0., 0, 0, 0], [1e-6, 0, 0, 1], [2e-6, 1, 0, 0]]
#     ne = np.array(e)
#     f.appendEvents(ne)
#     e = [[3e-6, 0, 0, 1], [5e-6, 0, 0, 1], [9e-6, 1, 0, 0]]
#     ne = np.array(e)
#     f.appendEvents(ne)
#     f.close()
