from setuptools import setup, find_packages

setup(name='MainStoinker', version='1.0', packages=find_packages(),
      extras_require={"web": ["fastapi==0.165.0", "uvicorn==0.54.0", "httpx==0.28.1"]},
      package_data={"MainStoinker.api": ["static/*", "static/**/*"]})
