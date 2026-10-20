"""任务子目录路径派生。"""

from pathlib import Path

from .errors import HLSException


def derive_task_paths(output_arg):
    """返回最终文件路径和缓存目录，不创建任何目录。"""
    output = Path(output_arg).expanduser().resolve()

    # -o output/name.mp4 时直接复用 output/，避免嵌套成 output/output/。
    if output.parent.name == output.stem:
        task_dir = output.parent
    else:
        task_dir = output.parent / output.stem

    return output, task_dir / "cache"


def resolve_task_paths(output_arg):
    """返回任务目录内的最终文件路径和缓存目录，并创建任务目录。"""
    output, cache = derive_task_paths(output_arg)
    task_dir = cache.parent

    if task_dir.exists() and not task_dir.is_dir():
        raise HLSException(f"任务目录路径不是目录：{task_dir}")

    task_dir.mkdir(parents=True, exist_ok=True)
    return output, cache
