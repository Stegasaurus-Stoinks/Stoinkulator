import datetime
from dateutil.relativedelta import *

LiveData = False
LiveTrading = False
#once trade excecution is figured out we should be able to get rid of one of these variables

FrontEndDisplay = True
#Frontend Variables
FrontEndPort = '24.56.52.6:3000'
updating = 1

#LiveData Variables
intraMinuteDisplay = True

#BackTesting Variables
StartDate = datetime.datetime.now() - relativedelta(month=0,weeks=0,day=2)
Duration = 2
TimeDelayPerPoint = 1 #seconds between backtested points