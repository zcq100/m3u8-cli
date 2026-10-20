class HLSException(RuntimeError):
    pass


class DownloadCancelled(HLSException):
    """任务被协作式取消。"""
