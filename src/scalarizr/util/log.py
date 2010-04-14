'''
Created on 22.01.2010

@author: shaitanich
'''
# TODO: store logs in database table 'log'. 
# `emit` should insert entries, `send_message` should select and delete entries
import atexit
import logging
import sqlite3
import threading
from scalarizr.bus import bus

try:
	import time
except ImportError:
	import timemodule as time

try:
	import cPickle as pickle
except:
	import pickle

class MessagingHandler(logging.Handler):
	def __init__(self, num_stored_messages = 1, send_interval = "1"):
		pool = bus.db
		self.conn = pool.get().get_connection()
		
		#self.conn.row_factory = sqlite3.Row	
		self.time_point = time.time()
		logging.Handler.__init__(self)
		self._msg_service = bus.messaging_service
		self.num_stored_messages = num_stored_messages
		
		if send_interval.endswith('s'):
			self.send_interval = int(send_interval[:-1])
		elif  send_interval.endswith('min'):
			self.send_interval = int(send_interval[:-3])*60
		elif send_interval.isdigit():
			self.send_interval = int(send_interval)
		else:
			self.send_interval = 1
		
		atexit.register(self.send_message)
		t = threading.Thread(target=self.timer_thread) 
		t.daemon = True
		t.start()

	def send_message(self):
		pool = bus.db
		connection = pool.get().get_connection()
		cur = connection.cursor()
		cur.execute("SELECT * FROM log")
		ids = []
		entries = []
		
		for row in cur.fetchall():
			args = pickle.loads(str(row['args']))
			exc_info = pickle.loads(str(row['exc_info']))
			entries.append((row['name'],row['level'],row['pathname'],row['lineno'],row['msg'],args,exc_info))
			ids.append(str(row['id']))
		cur.close()
			
		if entries:
			message = self._msg_service.new_message("LogMessage")
			producer = self._msg_service.get_producer()
			message.body["entries"] = entries
			producer.send(message)
			connection.execute("DELETE FROM log WHERE id IN (%s)" % (",".join(ids)))
			connection.commit()
		self.time_point = time.time()

	def emit(self, record):
		args = pickle.dumps(record.args) 
		exc_info = pickle.dumps(record.exc_info)
		data = (None, record.name, record.levelname, record.pathname, record.lineno, record.msg, args, exc_info)
		self.conn.execute('INSERT INTO log VALUES (?,?,?,?,?,?,?,?)', data)
		self.conn.commit()
		cur = self.conn.cursor()
		cur.execute("SELECT COUNT(*) FROM log")
		count = cur.fetchone()[0]
		cur.close()
		if count >= self.num_stored_messages:
			self.send_message()
		

	def timer_thread(self):
		while 1:
			while 1:
				time_delta = time.time() - self.time_point
				if  (time_delta > 1) and (time_delta > self.send_interval):
					break
				time.sleep(1)
			self.send_message()
			