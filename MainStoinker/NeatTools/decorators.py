import functools


"""
This file is for random neat decorators and should have NOTHING ELSE
"""


def debug(func):
    """Print the function signature and return value"""

    @functools.wraps(func)
    def wrapper_debug(*args, **kwargs):
        args_repr = [repr(a) for a in args]  # 1
        kwargs_repr = [f"{k}={v!r}" for k, v in kwargs.items()]  # 2
        signature = ", ".join(args_repr + kwargs_repr)  # 3
        print(f"Calling {func.__name__}({signature})")
        value = func(*args, **kwargs)
        print(f"{func.__name__!r} returned {value!r}")  # 4
        return value

    return wrapper_debug




def multiton(cls):
   instances = {}
   def getinstance(key):
      if key not in instances:
         instances[key] = cls(key)
      return instances[key]
   return getinstance


def singleton(class_):
   instances = {}

   def getinstance(*args, **kwargs):
      if class_ not in instances:
         instances[class_] = class_(*args, **kwargs)
      return instances[class_]

   return getinstance