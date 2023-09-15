import datetime
from dateutil.relativedelta import *

tickers = []
LiveData = True
LiveTrading = False
#once trade excecution is figured out we should be able to get rid of one of these variables

#Frontend Variables
FrontEndDisplay = True
FrontEndPort = '192.168.0.212:3000'
updating = 1

#LiveData Variables
intraMinuteDisplay = False

#BackTesting Variables
StartDate = datetime.datetime.now() - relativedelta(month=0,weeks=0,day=1)
Duration = 2 #days
TimeDelayPerPoint = 0 #seconds between backtested points

timedelaytesty = 0

