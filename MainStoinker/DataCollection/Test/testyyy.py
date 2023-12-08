import time
import threading

class player():
    def __init__(self):
        self._position = 1   
    @property
    def position(self):
        return self._position
   
    @position.setter
    def position(self, newPosition):
        self._position = newPosition
        time.sleep(1)
        print("execute more code here!")
        print("there")
        print(str(threading.current_thread()))

def temp():
    print('in temp')
    p.position = 2
    print("here")
    print(str(threading.current_thread()))
    


p = player()
print(p.position)
thr = threading.Thread(target=temp)
thr.start()


# p.position = 2
print("bleh")