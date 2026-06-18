"""
Loop 注册表：自动扫描 loops/ 下所有 BaseLoop 子类并实例化。
新增 loop 只需在 loops/ 下加一个文件，声明 name + description 即可。
"""

import importlib
import inspect
import pkgutil
from pathlib import Path

from .base import BaseLoop

_INSTANCE_CACHE: dict[str, BaseLoop] = {}


def discover() -> dict[str, BaseLoop]:
    """扫描 loops/ 下所有模块，返回 {name: instance}。带缓存，重复 name 启动报错。"""
    if _INSTANCE_CACHE:
        return _INSTANCE_CACHE

    package_dir = Path(__file__).parent
    for mod_info in pkgutil.iter_modules([str(package_dir)]):
        if mod_info.name in ("base",):
            continue
        mod = importlib.import_module(f".{mod_info.name}", __name__)
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is BaseLoop or not issubclass(obj, BaseLoop):
                continue
            instance = obj()
            if instance.name in _INSTANCE_CACHE:
                raise ValueError(f"loop name 重复: {instance.name}")
            _INSTANCE_CACHE[instance.name] = instance
    return _INSTANCE_CACHE
