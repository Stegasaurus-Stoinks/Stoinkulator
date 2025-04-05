import datetime
from dateutil.relativedelta import *
import logging

# Backend Variables
tickers = {}
algos = []
loggers = ["dum_dum"]
log_level = logging.INFO
LiveData = True
LiveTrading = False
#once trade excecution is figured out we should be able to get rid of one of these variables

#Apiapi debug
Debug = False

#Frontend Variables
FrontEndDisplay = False
FrontEndPort = '24.56.52.6:3000' # '192.168.1.108:3000'  # 
updating = 1
tickerIndex = 0
#LiveData Variables
intraMinuteDisplay = True

#BackTesting Variables
Duration = 1 #days
StartDate = datetime.datetime.now() - relativedelta(month=0,weeks=0,day=Duration)
TimeDelayPerPoint = 0 #seconds between backtested points

#offline dev
collectofflinedata = 0
offline = 0
