pushd %~dp0  & ::  added this line so that it runs correctly in task scheduler
git pull
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe "MainStoinker\MainStuff\this is how we do it dum dum.py"
pause