#!/usr/bin/python3

import time
import sys
from datetime import datetime
import csv
import threading
from multiprocessing import Process
import configparser
import fileinput

import RPi.GPIO as GPIO
import numpy as np
import os

import board
import busio
import adafruit_ads1x15.ads1015 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from adafruit_mcp230xx.mcp23017 import MCP23017
import digitalio

import pandas as pd
import matplotlib.pyplot as plt

# Needed for Slack Integration
# import slack
from slackclient import SlackClient

#Logging
import logging

import plotter
import glob


mstart_time = datetime.now()
config = configparser.ConfigParser()
config.read('eve-conf.ini')

totsys = (''.join(config.sections())).count('EVE')
actsys = []
for sysiter in range(totsys):
    if config['EVE' + str(sysiter+1)].getboolean('enabled'):
        actsys.append(sysiter+1)


# slack_client = slack.WebClient(token = config['MAIN']['slack_key'])
slack_client = SlackClient(config['MAIN']['slack_key'])
if slack_client.rtm_connect():
    print ('Multiplexer Started.')
    if (totsys == 1):
        multimess = slack_client.api_call(
            "chat.postMessage",
            username = config['MAIN']['hostname'],
            icon_url = config['MAIN']['multi_icon'],
            channel=config['MAIN']['slack_channel'],
            text = mstart_time.strftime('Started at %H:%M:%S on %a - %b %d, %Y. There is ' + str(totsys) + ' system configured.')
            )
    else:
        multimess = slack_client.api_call(
            "chat.postMessage",
            username = config['MAIN']['hostname'],
            icon_url = config['MAIN']['multi_icon'],
            channel=config['MAIN']['slack_channel'],
            text = mstart_time.strftime('Started at %H:%M:%S on %a - %b %d, %Y. There are ' + str(totsys) + ' systems configured.')
            )
else:
    sys.exit("No connection to Slack.")

chanid = multimess['channel']
multits = multimess['ts']

i2c_lock = [0]*totsys
i2c_q = []
graph_lock = [0]*totsys
graph_q = []
morbidostats = list()
comb_mesg = []
comb_saveloc = ''
comb_lat_sw = ['First','']

if config['MAIN'].getboolean('temp_sensor'): temp = 0.0

odcsvs = []
pumpcsvs = []

def IC_init():
    adc = list()
    gpioe = list()

    adc_add = list()
    gpio_add = list()

    for sysitr in range(totsys):
        sysnum = sysitr + 1
        confsec = 'EVE' + str(sysnum)
        if config[confsec].getboolean('enabled'):
            adc_add.append(config[confsec].getint('a_address'))
            if not config[confsec].getboolean('Pi_pins'):
                gpio_add.append(config[confsec].getint('m_address'))

    adc_add = list(set(adc_add))
    gpio_add = list(set(gpio_add))

    i2c = busio.I2C(board.SCL, board.SDA)

    if adc_add:
        for add in adc_add:
            adc.append(ADS.ADS1015(i2c, address= add))

    if gpio_add:
        for add in gpio_add:
            gpioe.append(MCP23017(i2c, address=add))

    return {'adc':adc, 'gpioe':gpioe, 'adc_add':adc_add, 'gpio_add':gpio_add}

def eve_starter():
    for sysitr in range(totsys):
        sysnum = sysitr + 1
        confsec = 'EVE' + str(sysnum)
        if config[confsec].getboolean('enabled') is True:
            print (confsec + ' enabled.')
            morbidostats.append([Morbidostat(sysnum, len(actsys), chips, slack_client), sysnum])
            #Morbidostat(sysnum)
            # thread.join
        else:
            print (confsec + ' not enabled. Skipping.')
            slackms = slack_client.api_call(
                "chat.postMessage",
                username = config['MAIN']['hostname'],
                icon_url = config['MAIN']['multi_icon'],
                channel = config['MAIN']['slack_channel'],
                text = confsec + ' is not enabled. Skipping.'
                )

    print ('Starting EVEs')
    for starti in range(len(morbidostats)):
       morbidostats[starti][0].start()

    if config['MAIN'].getboolean('comb_graph') and len(actsys) > 1:
        combgen = slack_client.api_call(
            "chat.postMessage",
            username = config['MAIN']['hostname'],
            icon_url = config['MAIN']['multi_icon'],
            channel = config['MAIN']['slack_channel'],
            text = 'Combined Graphs'
            )
        comblat = slack_client.api_call(
            "chat.postMessage",
            username = config['MAIN']['hostname'],
            icon_url = config['MAIN']['multi_icon'],
            channel = config['MAIN']['slack_channel'],
            text = 'Latest Combined Graphs'
            )

        global comb_mesg
        comb_mesg = [combgen['ts'], comblat['ts']]


def graph_controller():
    while True:
        if len(graph_q) is 0:
            time.sleep(20)
        else:
            if graph_q[0] is 'C':
                comb_grapher()
            else:
                morbidostats[graph_q[0]][0].graphOD()
            graph_q.pop(0)

def i2c_controller():
    while True:
        if len(i2c_q) is 0:
            time.sleep(0.05)
        else:
            if i2c_q[0][1] is 'O':
                morbidostats[int(i2c_q[0][0])][0].get_OD()
            elif i2c_q[0][1] is 'C':
                morbidostats[int(i2c_q[0][0])][0].control_alg()
            i2c_q.pop(0)


def live_plotter():
    max_time = 0
    for sysitr in range(totsys):
        sysnum = sysitr + 1
        confsec = 'EVE' + str(sysnum)
        if config[confsec].getboolean('enabled') is True:
            temp_time = config[confsec].getfloat('time_between_saves')
            if temp_time > max_time:
                max_time = temp_time

    time.sleep(max_time*60+5)

    global odcsvs
    global pumpcsvs
    for starti in range(len(morbidostats)):
       temp_locs = morbidostats[starti][0].file_locs()
       odcsvs.append(temp_locs['ods'])
       pumpcsvs.append(temp_locs['pumps'])

    Process(target = plotter.Plotter, args = (actsys, odcsvs, pumpcsvs, config['MAIN']['hostname'])).start()

    if config['MAIN'].getboolean('comb_graph') and len(actsys) > 1: threading.Thread(target = comb_graph_scheduler).start()

def comb_graph_scheduler():
    global comb_saveloc
    root_dir = config['MAIN']['save_location']
    comb_saveloc = root_dir + '/Combined/' + str(datetime.now()) + '/'
    os.makedirs(comb_saveloc)

    while True:
        time.sleep(config['MAIN'].getfloat('comb_graph_freq')*60)
        global graph_q
        graph_q.append('C')

def comb_grapher():
    ods = []
    leg = []
    print('Generating Combined Graphs')

    fig = plt.figure(dpi=140)
    ax = plt.gca()
    for i in actsys: leg.append('EVE'+str(i))
    for i in odcsvs:
        ods.append(pd.read_csv(i,index_col='hour'))
        ods[-1][['average']].plot(ax=ax,figsize=(7,5))
    ax.legend(leg)
    ax.set_ylabel('Raw OD')
    ax.set_xlabel('Time(h)')
    global comb_saveloc
    fig.savefig(comb_saveloc + 'RawOD.png')

    fig2 = plt.figure(dpi=140)
    ax2 = plt.gca()
    for i in ods:
        i[['average']].divide(float(i.iloc[-1][['maxod']])).plot(ax=ax2,figsize=(7,5))
    ax2.legend(leg)
    ax2.set_ylabel('Scaled OD')
    ax2.set_xlabel('Time(h)')
    fig2.savefig(comb_saveloc + 'ScaledOD.png')

    global comb_mesg
    global comb_lat_sw
    with open(comb_saveloc + 'RawOD.png', "rb") as file_content:
        combgen_pic = slack_client.api_call(
            "files.upload",
            channels = config['MAIN']['slack_channel'],
            thread_ts = comb_mesg[0],
            title = "RawOD",
            file = file_content
        )
    with open(comb_saveloc + 'ScaledOD.png', "rb") as file_content:
        combgen_pics = slack_client.api_call(
            "files.upload",
            channels = config['MAIN']['slack_channel'],
            thread_ts = comb_mesg[0],
            title = "ScaledOD",
            file = file_content
        )

    if comb_lat_sw[0] is 'First':
        with open(comb_saveloc + 'RawOD.png', "rb") as file_content:
            comblat_pic = slack_client.api_call(
                "files.upload",
                channels = config['MAIN']['slack_channel'],
                thread_ts = comb_mesg[1],
                title = "RawOD",
                file = file_content
            )
        with open(comb_saveloc + 'ScaledOD.png', "rb") as file_content:
            comblat_pics = slack_client.api_call(
                "files.upload",
                channels = config['MAIN']['slack_channel'],
                thread_ts = comb_mesg[1],
                title = "RawOD",
                file = file_content
            )
        comb_lat_sw = [comblat_pic['file']['shares']['public'][chanid][0]['ts'], comblat_pics['file']['shares']['public'][chanid][0]['ts']]
    else:
        delcomb = slack_client.api_call(
            "chat.delete",
            channel = chanid,
            ts = comb_lat_sw[0]
            )
        delcombs = slack_client.api_call(
            "chat.delete",
            channel = chanid,
            ts = comb_lat_sw[1]
            )

        with open(comb_saveloc + 'RawOD.png', "rb") as file_content:
            comblat_pic = slack_client.api_call(
                "files.upload",
                channels = config['MAIN']['slack_channel'],
                thread_ts = comb_mesg[1],
                title = "RawOD",
                file = file_content
            )
        with open(comb_saveloc + 'ScaledOD.png', "rb") as file_content:
            comblat_pics = slack_client.api_call(
                "files.upload",
                channels = config['MAIN']['slack_channel'],
                thread_ts = comb_mesg[1],
                title = "RawOD",
                file = file_content
            )
        comb_lat_sw = [comblat_pic['file']['shares']['public'][chanid][0]['ts'], comblat_pics['file']['shares']['public'][chanid][0]['ts']]




def slackresponder():
    while True:
        try:
            events = slack_client.rtm_read()
            for event in events:
                for sysitr in range(len(morbidostats)):
                    sysnum = morbidostats[sysitr][1]
                    evename = 'EVE' + str(sysnum)
                    if (
                        event.get('channel') == chanid and
                        event.get('text') == evename and
                        event.get('thread_ts') == multits and
                        event.get('type') == 'message'
                    ):
                        # print(event)
                        respmsg = slack_client.api_call(
                            "chat.postMessage",
                            username = 'Multiplexer',
                            icon_url = config['MAIN']['multi_icon'],
                            channel=mchan,
                            text = 'Generating Graphs for ' + evename,
                            thread_ts= multits
                            )
                        morbidostats[sysitr][0].graphOD()
            time.sleep(60)
        except KeyboardInterrupt:
            break
        except Exception as e:
            # slack_client.api_call(
                # "chat.postMessage",
                # username = 'Multiplexer',
                # icon_url = config['MAIN']['multi_icon'],
                # channel=mchan,
                # text = 'Slack Reponder *o*',
                # thread_ts= multits
                # )
            # slack_client.api_call(
                # "chat.postMessage",
                # username = 'Multiplexer',
                # icon_url = config['MAIN']['multi_icon'],
                # channel=mchan,
                # text = e,
                # thread_ts= multits
                # )
            pass

# def temp_runner():
    # if config['MAIN'].getboolean('temp_sensor'):
        # while True:
            # i2c_q.append('TT')
            # time.sleep(3)


def temp_sensor_func():
        base_dir = '/sys/bus/w1/devices/'
        device_folder = glob.glob(base_dir + '28*')[0]
        device_file = device_folder + '/w1_slave'

    while True:
        f = open(device_file, 'r')
        lines = f.readlines()
        f.close()

        while lines[0].strip()[-3:] != 'YES':
            time.sleep(0.2)
            lines = read_temp_raw()
        equals_pos = lines[1].find('t=')
        if equals_pos != -1:
                temp_string = lines[1][equals_pos+2:]
                global temp
                temp = float(temp_string) / 1000.0
        time.sleep(3)



class Morbidostat:
    def __init__(self, sysnum, actsys, chips, slack_client):
        self.printing = False
        self.sysnum = sysnum
        self.actsys = actsys
        self.adc= chips['adc']
        self.gpioe = chips['gpioe']
        self.adc_add = chips['adc_add']
        self.gpio_add = chips['gpio_add']
        self.sysstr = 'EVE' + str(self.sysnum)

        self.threads = {}
        self.thread_locks = {'save' : threading.Lock(), 'adc' : threading.Lock(), 'dynL' : threading.Lock(), 'control_alg' : threading.Lock(), 'graphs' : threading.Lock(), 'threads' : threading.Lock()}

        self.config = configparser.ConfigParser()
        self.config.read('eve-conf.ini')

        # Define Experiment Variables
        self.time_between_pumps = self.config[self.sysstr].getfloat('time_between_pumps')
        self.OD_thr = self.config[self.sysstr].getfloat('OD_thr')
        self.OD_thr_set = False
        self.OD_min = self.config[self.sysstr].getfloat('OD_min')
        self.OD_err = self.config[self.sysstr].getfloat('OD_error')
        self.time_between_ODs = self.config[self.sysstr].getfloat('time_between_ODs') # how often to gather OD data, in seconds
        self.time_between_graphs = self.config[self.sysstr].getfloat('time_between_graphs') # how often to graph, in minutes
        # OD_thr is the threshold above which to activate drug pump  [vish bench tests: empty: 3.5V, Clear Vial: 0.265V, Very Cloudy Vial: 2.15V]

        #time_between_writes = 1  # how often to write out OD data, in minutes
        #loops_between_writes = (time_between_writes*60)/time_between_ODs # time bewteen writes in loops
        self.time_between_saves = self.config[self.sysstr].getfloat('time_between_saves')

        # Set Up I2C to Read OD Data
        # Create the I2C bus

        self.P_drug_times = self.config[self.sysstr].getfloat('P_drug_times')
        self.P_nut_times = self.config[self.sysstr].getfloat('P_nut_times')
        self.P_waste_times = self.config[self.sysstr].getfloat('P_waste_times')

        self.running_data = []  # the list which will hold our 2-tuples of time and OD
        self.pump_data = []
        self.OD_tmplist = []
        self.pump_tmplist = []
        self.hr_OD_tmplist = []
        self.hr_pump_tmplist = []

        self.root_dir = self.config['MAIN']['save_location']
        # self.currOD = np.zeros(num_cham)
        self.currOD = 0
        # averaged OD value
        self.scaling = self.config[self.sysstr].getboolean('scaling')
        self.avOD = 0
        self.maxOD = 0
        self.OD_av_length = self.config[self.sysstr].getint('OD_av_length')
        # OD averaging buffer
        self.avOD_buffer = [0] * self.OD_av_length #need to change for multiplexing
        self.thresh_check = self.config[self.sysstr].getfloat('time_thresh')
        self.growthOD = []
        self.growthrate = []
        self.growthrate2 = []
        self.growthrate_t = []
        self.avefac = 30
        self.instant_gr = 0
        self.instant_gr2 = 0

        self.graph_loops = self.actsys * self.config['MAIN'].getint('graph_resolution_fac')
        self.elapsed_loop_time = 0
        self.loops = 0
        self.last_dilutionOD = 0
        self.last_increase = 0
        self.nut = 0
        self.drug = 1
        self.waste = 2
        self.max_nut = self.nut
        self.max_drug = self.drug
        self.max_waste = self.waste

        self.vial_drug_mass = 0

        self.temp_sensor = self.config['MAIN'].getboolean('temp_sensor')

        self.total_time = self.config[self.sysstr].getfloat('Exp_time_hours')*3600 #in seconds
        self.loops_between_ODs = 1
        self.loops_between_pumps = (self.time_between_pumps*60)/self.time_between_ODs # time between pumps in loops

        # num_cham = 1 # number of morbidostat vials being used

        self.photod = AnalogIn(self.adc[self.adc_add.index(self.config[self.sysstr].getint('a_address'))], getattr(ADS,'P'+ str(self.config[self.sysstr].getint('Analogin'))))

        # Setup the GPIO Pins to Control the Pumps
        self.pipins = self.config[self.sysstr].getboolean('pi_pins')
        self.P_drug_pins = self.config[self.sysstr].getint('P_drug_pins')
        self.P_nut_pins = self.config[self.sysstr].getint('P_nut_pins')
        self.P_waste_pins = self.config[self.sysstr].getint('P_waste_pins')
        self.P_LED_pins = self.config[self.sysstr].getint('P_LED_pins')
	# P_fan_pins = self.config[self.sysstr].getint('P_fan_pins')
        self.pin_list = [self.P_drug_pins, self.P_nut_pins, self.P_waste_pins, self.P_LED_pins]

        if self.pipins:
            GPIO.setmode(GPIO.BCM)
            for pin in self.pin_list:
                GPIO.setup(pin, GPIO.OUT)
        else:
            self.pins = [None]*(max(self.pin_list)+1)
            self.mcp = self.gpioe[self.gpio_add.index(self.config[self.sysstr].getint('m_address'))]

            for pin in self.pin_list:
                self.pins[pin] = self.mcp.get_pin(pin)
                self.pins[pin].direction = digitalio.Direction.OUTPUT
                self.pins[pin].value = False

        self.init_time = datetime.now()

        self.drug_name = self.config[self.sysstr]['drug']
        self.drug_conc = self.config[self.sysstr].getfloat('drug_conc')
        self.drug_vol = self.config[self.sysstr].getfloat('drug_vol')

        self.slack_client = SlackClient(self.config['MAIN']['slack_key'])
        # self.slack_client = slack.WebClient(token = config['MAIN']['slack_key'])
        self.slack_usericon = self.config[self.sysstr]['slack_icon']
        self.chan = self.config['MAIN']['slack_channel']
        initmsg = self.slack_client.api_call(
            "chat.postMessage",
            channel = self.chan,
            username=self.sysstr,
            icon_url = self.slack_usericon,
            text = self.init_time.strftime('Initialized at %H:%M:%S')
            )

    def start(self):
        self.start_time = datetime.now()
        if self.root_dir[-1] == '/': self.root_dir.pop(-1)
        os.makedirs(self.root_dir + "/" + self.sysstr + "/" + str(self.start_time))

        # self.elogr = logging.getLogger('self.elogr')
        # self.elogr.setLevel(logging.DEBUG)
        # self.elogrfh = logging.FileHandler('%s/%s/%s/exceptions.txt' % (self.root_dir, self.sysstr, self.start_time))
        # self.elogrfh.setFormatter("%(asctime)s — %(name)s — %(levelname)s — %(message)s")
        # self.elogr.addHandler(self.elogrfh)

        # self.ilogr = logging.getLogger('self.ilogr')
        # self.ilogr.setLevel(logging.INFO)
        # self.ilogrfh = logging.FileHandler('%s/%s/%s/info.txt' % (self.root_dir, self.sysstr, self.start_time))
        # self.ilogrfh.setFormatter("%(asctime)s — %(name)s — %(levelname)s — %(message)s")
        # self.ilogr.addHandler(self.ilogrfh)

        self.outfile_OD = "%s/%s/%s/ODdata_%s.csv" % (self.root_dir, self.sysstr, self.start_time, self.start_time)
        file = open(self.outfile_OD, 'a')
        wr = csv.writer(file)
        # wr.writerow(['Current OD', 'Average OD','OD Timing'])
        if self.temp_sensor:
            wr.writerow(['current','average','maxod','time','hour','temp','threads','min'])
        else:
            wr.writerow(['current','average','maxod','time','hour','threads','min'])
        file.close()

        self.outfile_pump = "%s/%s/%s/pump_%s.csv" % (self.root_dir, self.sysstr, self.start_time, self.start_time)
        file = open(self.outfile_pump, 'a')
        wr = csv.writer(file)
        # wr.writerow(['Nutrient Pump', 'Drug Pump','Waste Pump','Pump Timing', 'Drug Mass'])
        wr.writerow(['media', 'drug','waste','pump_time','hour','vial_drug_mass'])
        file.close()

        #Detailed Files
        self.hr_outfile_OD = "%s/%s/%s/hr_ODdata_%s.csv" % (self.root_dir, self.sysstr, self.start_time, self.start_time)
        file = open(self.hr_outfile_OD, 'a')
        wr = csv.writer(file)
        # wr.writerow(['Current OD', 'Average OD','OD Timing'])
        if self.temp_sensor:
            wr.writerow(['current','average','maxod','time','hour','temp','threads','min'])
        else:
            wr.writerow(['current','average','maxod','time','hour','threads','min'])
        file.close()

        self.hr_outfile_pump = "%s/%s/%s/hr_pump_%s.csv" % (self.root_dir, self.sysstr, self.start_time, self.start_time)
        file = open(self.hr_outfile_pump, 'a')
        wr = csv.writer(file)
        # wr.writerow(['Nutrient Pump', 'Drug Pump','Waste Pump','Pump Timing', 'Drug Mass'])
        wr.writerow(['media', 'drug','waste','pump_time','hour','vial_drug_mass'])
        file.close()




        #TURN ON THE FAN HERE

        # print('Experiment begun at %02s:%02s:%02s' % (self.start_time.hour, self.start_time.minute, self.start_time.second))
        print(self.start_time.strftime(self.sysstr + ' started at %H:%M:%S on %a - %b %d, %Y'))
        # self.ilogr.info(self.start_time.strftime(self.sysstr + ' started at %H:%M:%S on %a - %b %d, %Y'))
        threading.Thread(target=self.on_timer).start()

        self.initalmessage = self.slack_client.api_call(
            "chat.postMessage",
            channel = self.chan,
            username=self.sysstr,
            icon_url = self.slack_usericon,
            text = self.start_time.strftime('Experiment started at %H:%M:%S on %a - %b %d, %Y')
            )

        self.recgra = self.slack_client.api_call(
            "chat.postMessage",
            channel = self.chan,
            username=self.sysstr,
            icon_url = self.slack_usericon,
            text = self.start_time.strftime('Most recent graphs')
            )


        # self.history = self.slack_client.api_call("channels.history", channel=self.chanid, count = 1)
        # self.threadts = self.history['messages'][0]['ts']
        self.chanid = self.initalmessage['channel']
        self.threadts = self.initalmessage['ts']
        self.recgrats = self.recgra['ts']
        self.firstrec = True

    def get_OD(self):

        print_buffer = 0

        try:
            if self.pipins:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.P_LED_pins, GPIO.OUT)

                GPIO.output(self.P_LED_pins,1)
                time.sleep(0.1)
                self.currOD = self.photod.voltage #np.asarray(self.value)#[0]
                time.sleep(0.1)
                GPIO.output(self.P_LED_pins,0)
            else:
                self.pins = [None]*(max(self.pin_list)+1)
                self.mcp = self.gpioe[self.gpio_add.index(self.config[self.sysstr].getint('m_address'))]
                self.pins[self.P_LED_pins] = self.mcp.get_pin(self.P_LED_pins)
                self.pins[self.P_LED_pins].direction = digitalio.Direction.OUTPUT

                self.pins[self.P_LED_pins].value = True
                time.sleep(0.1)
                self.currOD = self.photod.voltage #np.asarray(self.value)#[0]
                time.sleep(0.1)
                self.pins[self.P_LED_pins].value = False
        except:
            print ('[%s] OD - WARNING ADC REQUEST CRASHED' % self.sysstr)
            pass

        self.avOD_buffer = self.avOD_buffer + [self.currOD]
        self.avOD_buffer.pop(0)
        self.avOD = sum(self.avOD_buffer)/len(self.avOD_buffer)
        if self.avOD > self.maxOD: self.maxOD = self.avOD

        self.thread_locks['adc'].release()

    def pump_on(self,pump):
        if self.pipins:
            GPIO.output(pump, 1)
        else:
            self.pins[pump].value = True
        print('[%s] Turning on pump %s' % (self.sysstr,pump))

    def pump_off(self,pump):
        if self.pipins:
            GPIO.output(pump, 0)
        else:
            self.pins[pump].value = False
        print('[%s] Turning off pump %s' % (self.sysstr,pump))

    def all_pump_off(self):
        if self.pipins:
            for i in pin_list:
                GPIO.output(i, 0)
        else:
            for i in pin_list:
                self.pins[i].value = False
        print('[%s] Turning off all pump' % (self.sysstr,pump))

    def file_locs(self):
        return {'ods':self.outfile_OD, 'pumps': self.outfile_pump}

    def bufferdata(self):
        if self.temp_sensor:
            global temp
            odlist = [self.currOD, self.avOD, self.maxOD, self.nows, (self.elapsed_time.total_seconds())/3600, temp, self.active_threads, self.OD_min]
            self.hr_OD_tmplist.append(odlist)
        else:
            odlist = [self.currOD, self.avOD, self.maxOD, self.nows, (self.elapsed_time.total_seconds())/3600, self.active_threads, self.OD_min]
            self.hr_OD_tmplist.append(odlist)
        pulist = [self.nut,self.drug,self.waste,self.nows,(self.elapsed_time.total_seconds())/3600,self.vial_drug_mass]
        self.hr_pump_tmplist.append(pulist)
        if self.max_nut < self.nut: self.max_nut = self.nut
        if self.max_drug < self.drug: self.max_drug = self.drug
        if self.max_waste < self.waste: self.max_waste = self.waste
        self.nut = 0
        self.drug = 1
        self.waste = 2
        if (self.loops % self.graph_loops) == 0:
            pulist = [self.max_nut,self.max_drug,self.max_waste,self.nows,(self.elapsed_time.total_seconds())/3600,self.vial_drug_mass]
            self.OD_tmplist.append(odlist)
            self.pump_tmplist.append(pulist)
            self.max_nut = self.nut
            self.max_drug = self.drug
            self.max_waste = self.waste

    def savefunc(self):
        self.thread_locks['save'].acquire()
        self.bufferdata()

        with open(self.hr_outfile_OD, 'a') as file:
            wr = csv.writer(file)
            wr.writerows(self.hr_OD_tmplist)
            file.close()

        with open(self.hr_outfile_pump, 'a') as file:
            wr = csv.writer(file)
            wr.writerows(self.hr_pump_tmplist)
            file.close()

        with open(self.outfile_OD, 'a') as file:
            wr = csv.writer(file)
            wr.writerows(self.OD_tmplist)
            file.close()

        with open(self.outfile_pump, 'a') as file:
            wr = csv.writer(file)
            wr.writerows(self.pump_tmplist)
            file.close()

        self.OD_tmplist = []
        self.pump_tmplist = []
        self.hr_OD_tmplist = []
        self.hr_pump_tmplist = []
        self.thread_locks['save'].release()

    def graphOD(self):

        print('[%s] Generating graph' % self.sysstr)

        try:
            elapmsg = self.slack_client.api_call(
                "chat.postMessage",
                channel = self.chan,
                username=self.sysstr,
                icon_url = self.slack_usericon,
                text = ('Elapsed Time: %s ; OD = %.3f' % (self.secondsToText(int(self.elapsed_time.total_seconds())),self.currOD)),
                thread_ts = self.threadts
                )

            allODs = pd.read_csv(self.outfile_OD, index_col='hour')

            if self.scaling: allODs[['average']] = allODs[['average']]/float(allODs[['maxod']].iloc[-1])
            if self.scaling: allODs[['min']] = allODs[['min']]/float(allODs[['maxod']].iloc[-1])

            # allODs['hour'] = allODs['time'] - allODs['time'].iloc[0]
            # allODs['hour'] = allODs['hour'].divide(3600)
            # allODs.set_index('hour')
            # print(allODs)
            #fig = plt.figure(dpi=1000)
            plt.rcParams["figure.dpi"] = 200
            ODplt = (allODs[['average']]).plot()  #figsize=(10,10) in the plot
            # ODplt = (allODs[['current']]).plot()  #figsize=(10,10) in the plot
            ODfig = ODplt.get_figure()
            self.outfile_OD = "%s/%s/%s/ODdata_%s.csv" % (self.root_dir, self.sysstr, self.start_time, self.start_time)
            ODfig.savefig("%s/%s/%s/ODplot_%s.png"  % (self.root_dir, self.sysstr, self.start_time, self.start_time))
            ODfig.clf(); ODplt = None; ODfig = None; fig = None
            with open("%s/%s/%s/ODplot_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                odmsg = self.slack_client.api_call(
                    "files.upload",
                    channels = self.chan,
                    thread_ts = self.threadts,
                    title = "ODPlot",
                    file = file_content
                )

            allpumps = pd.read_csv(self.outfile_pump, index_col='hour')   # cols: 'media', 'drug','waste','pump_time','hour','vial_drug_mass'

            allconcs = allpumps[['vial_drug_mass']]/12
            allconcs.rename(columns={'vial_drug_mass':'drug_conc'}, inplace=True)
            # allODs['hour'] = allODs['time'] - allODs['time'].iloc[0]
            # allODs['hour'] = allODs['hour'].divide(3600)
            # allODs.set_index('hour')
            # print(allODs)
            #fig = plt.figure(dpi=1000)
            plt.rcParams["figure.dpi"] = 200
            ODplt = (allODs[['average']]).plot(label='average', color='tab:blue')  #figsize=(10,10) in the plot
            ODplt.set_ylabel(ylabel='Average OD')
            lines, labels = ODplt.get_legend_handles_labels()

            DM = ODplt.twinx()
            DM.spines['right'].set_position(('axes', 1.0))
            allconcs.plot(ax = DM, label='vial_drug_mass',color='tab:orange',legend=False)
            DM.set_ylabel('%s Concentration (ug/mL)' % self.drug_name.capitalize())
            line, label = DM.get_legend_handles_labels()
            lines += line
            labels += label
            ODplt.legend(lines, labels, loc=2)
            # ODplt = (allODs[['current']]).plot()  #figsize=(10,10) in the plot
            ODfig = ODplt.get_figure()
            ODfig.savefig("%s/%s/%s/ODconc_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), bbox_inches='tight')
            ODfig.clf(); ODplt.figure = None; ODplt = None; ODfig = None; fig = None; allconcs= None; colors = None; DM = None
            plt.close('all')
            with open("%s/%s/%s/ODconc_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                concmsg = self.slack_client.api_call(
                    "files.upload",
                    channels = self.chan,
                    thread_ts = self.threadts,
                    title = "ODConc",
                    file = file_content
                )


            pumpa = allpumps[['media','drug','waste']]
            PUplt,PUax = plt.subplots()
            PUax.plot(allODs[['average']], label= 'average', color='tab:blue')
            PUax.plot(allODs[['min']], label= '_nolegend_', color = 'tab:grey', linestyle= ':')
            PUax.set_ylabel(ylabel='Average OD')
            lines, labels = PUax.get_legend_handles_labels()

            DM = PUax.twinx()
            DM.spines['right'].set_position(('axes', 1.0))
            pumpa.plot(ax = DM,color=['tab:orange','tab:red','tab:green'],legend=False)
            DM.set_yticklabels([])

            line, label = DM.get_legend_handles_labels()
            lines += line
            labels += label
            PUax.legend(lines, labels, loc=2)
            # PUplt.axhline(y=self.OD_min, color='tab:grey', linestyle=':')
            # PUplt.axhline(y=self.OD_thr, color='tab:grey', linestyle=':')

            # PUfig = PUplt.get_figure()
            PUplt.savefig("%s/%s/%s/PUplot_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time))
            allpumps = None; PUplt.figure = None; PUplt = None; allconcs= None; colors = None; DM = None; pumpa = None
            plt.close('all')
            with open("%s/%s/%s/PUplot_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                pumsg = self.slack_client.api_call(
                    "files.upload",
                    channels = self.chan,
                    thread_ts = self.threadts,
                    title = "PUPlot",
                    file = file_content
                )

            # THREADS GRAPH

            plt.rcParams["figure.dpi"] = 200
            ODthr = (allODs[['average']]).plot(label='average', color='tab:blue')  #figsize=(10,10) in the plot
            ODthr.set_ylabel(ylabel='Average OD')
            lines, labels = ODthr.get_legend_handles_labels()

            DM = ODthr.twinx()
            DM.spines['right'].set_position(('axes', 1.0))
            allODs[['threads']].plot(ax = DM, label='threads',color='tab:purple',legend=False)
            DM.set_ylabel(ylabel='Active Threads')
            line, label = DM.get_legend_handles_labels()
            lines += line
            labels += label
            ODthr.legend(lines, labels, loc=2)
            # ODplt = (allODs[['current']]).plot()  #figsize=(10,10) in the plot
            ODfig = ODthr.get_figure()
            ODfig.savefig("%s/%s/%s/ODthreads_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time))
            ODfig.clf(); ODthr.figure = None; ODthr = None; ODfig = None; fig = None; allconcs= None; colors = None; DM = None
            plt.close('all')
            with open("%s/%s/%s/ODthreads_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                thrmsg = self.slack_client.api_call(
                    "files.upload",
                    channels = self.chan,
                    thread_ts = self.threadts,
                    title = "ODThreads",
                    file = file_content
                )


            # TEMP GRAPH

            if self.temp_sensor:
                plt.rcParams["figure.dpi"] = 200
                ODthr = (allODs[['average']]).plot(label='average', color='tab:blue')  #figsize=(10,10) in the plot
                ODthr.set_ylabel(ylabel='Average OD')
                lines, labels = ODthr.get_legend_handles_labels()

                DM = ODthr.twinx()
                DM.spines['right'].set_position(('axes', 1.0))
                allODs[['temp']].plot(ax = DM, label='threads',color='tab:pink',legend=False)
                DM.set_ylabel(ylabel='Incubator Temperature (C)')
                line, label = DM.get_legend_handles_labels()
                lines += line
                labels += label
                ODthr.legend(lines, labels, loc=2)
                # ODplt = (allODs[['current']]).plot()  #figsize=(10,10) in the plot
                ODfig = ODthr.get_figure()
                ODfig.savefig("%s/%s/%s/ODtemp_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), bbox_inches='tight')
                ODfig.clf(); allODs = None; ODthr.figure = None; ODthr = None; ODfig = None; fig = None; allconcs= None; colors = None; DM = None
                plt.close('all')
                with open("%s/%s/%s/ODtemp_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    tempmsp = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.threadts,
                        title = "ODTemp",
                        file = file_content
                    )

            if self.firstrec:
                self.recmes = self.slack_client.api_call(
                    "chat.postMessage",
                    channel = self.chan,
                    username=self.sysstr,
                    icon_url = self.slack_usericon,
                    text = ('Elapsed Time: %s ; OD = %.3f' % (self.secondsToText(int(self.elapsed_time.total_seconds())),self.currOD)),
                    thread_ts = self.recgrats
                    )
                with open("%s/%s/%s/ODplot_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    self.recod = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.recgrats,
                        title = "ODPlot",
                        file = file_content
                    )
                with open("%s/%s/%s/ODconc_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    self.recodc = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.recgrats,
                        title = "ODConc",
                        file = file_content
                    )
                with open("%s/%s/%s/PUplot_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    self.recpu = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.recgrats,
                        title = "PUPlot",
                        file = file_content
                    )
                with open("/%s/%s/%s/ODthreads_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    self.rethr = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.recgrats,
                        title = "ODThreads",
                        file = file_content
                    )
                if self.temp_sensor:
                    with open("%s/%s/%s/ODtemp_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                        self.retmp = self.slack_client.api_call(
                            "files.upload",
                            channels = self.chan,
                            thread_ts = self.recgrats,
                            title = "ODTemp",
                            file = file_content
                        )
                # print(self.recod['file']['shares']['public'][self.chanid][0]['ts'])
                self.firstrec = False
            else:
                delmsg = self.slack_client.api_call(
                    "chat.delete",
                    channel = self.chanid,
                    ts = self.recmes['ts']
                    )
                delod = self.slack_client.api_call(
                    "chat.delete",
                    channel = self.chanid,
                    ts = self.recod['file']['shares']['public'][self.chanid][0]['ts']
                    )
                delodc = self.slack_client.api_call(
                    "chat.delete",
                    channel = self.chanid,
                    ts = self.recodc['file']['shares']['public'][self.chanid][0]['ts']
                    )
                delrec = self.slack_client.api_call(
                    "chat.delete",
                    channel = self.chanid,
                    ts = self.recpu['file']['shares']['public'][self.chanid][0]['ts']
                    )
                delthr = self.slack_client.api_call(
                    "chat.delete",
                    channel = self.chanid,
                    ts = self.rethr['file']['shares']['public'][self.chanid][0]['ts']
                    )
                if self.temp_sensor:
                    deltmp = self.slack_client.api_call(
                        "chat.delete",
                        channel = self.chanid,
                        ts = self.retmp['file']['shares']['public'][self.chanid][0]['ts']
                        )
                self.recmes = self.slack_client.api_call(
                    "chat.postMessage",
                    channel = self.chan,
                    username=self.sysstr,
                    icon_url = self.slack_usericon,
                    text = ('Elapsed Time: %s ; OD = %.3f' % (self.secondsToText(int(self.elapsed_time.total_seconds())),self.currOD)),
                    thread_ts = self.recgrats
                    )
                with open("%s/%s/%s/ODplot_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    self.recod = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.recgrats,
                        title = "ODPlot",
                        file = file_content
                    )
                with open("%s/%s/%s/ODconc_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    self.recodc = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.recgrats,
                        title = "ODConc",
                        file = file_content
                    )
                with open("%s/%s/%s/PUplot_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    self.recpu = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.recgrats,
                        title = "PUPlot",
                        file = file_content
                    )
                with open("%s/%s/%s/ODthreads_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                    self.rethr = self.slack_client.api_call(
                        "files.upload",
                        channels = self.chan,
                        thread_ts = self.recgrats,
                        title = "ODThreads",
                        file = file_content
                    )
                if self.temp_sensor:
                    with open("%s/%s/%s/ODtemp_%s.png" % (self.root_dir, self.sysstr, self.start_time, self.start_time), "rb") as file_content:
                        self.retmp = self.slack_client.api_call(
                            "files.upload",
                            channels = self.chan,
                            thread_ts = self.recgrats,
                            title = "ODTemp",
                            file = file_content
                        )
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            pass

        self.thread_locks['graphs'].release()

    def dynLimit(self):
        self.thread_locks['dynL'].acquire()
        self.growthOD.append(self.avOD)
        self.growthrate_t.append((self.elapsed_time.total_seconds()/3600))

        if len(self.growthOD) == self.avefac:
            god_temp = np.diff(self.growthOD)/np.diff(self.growthrate_t)
            self.growthrate.append(sum(god_temp)/len(god_temp))
            self.growthOD.pop(0)
            if len(self.growthrate) < self.avefac:
                self.growthrate_t.pop(0)

        if len(self.growthrate) == self.avefac:
            gr_temp = np.diff(self.growthrate)/np.diff(self.growthrate_t)
            self.growthrate2.append(sum(gr_temp)/len(gr_temp))
            self.growthrate.pop(0)
            self.growthrate_t.pop(0)

        if len(self.growthrate2) == self.avefac:
            self.instant_gr = sum(god_temp)/len(god_temp)
            self.instant_gr2 = sum(gr_temp)/len(gr_temp)
            self.growthrate2.pop(0)

        if self.instant_gr > self.OD_err and self.instant_gr2 < 0.01:
            self.OD_thr_set = True
            self.OD_min = self.avOD
            self.OD_thr = self.OD_min*1.25

        self.thread_locks['dynL'].release()

    def control_alg(self):
        print_buffer = 0
        # id = str(self.sysnum)+'CA'

        # i2c_q.append(id)

        # while i2c_q[0] is not id:
            # time.sleep(0.1)
            # print_buffer += 1
            # if print_buffer % 10 == 0: print ('[%s] {CAlg} Waiting for Locks...' % self.sysstr)

        # if i2c_q[0] is id:
            # i2c_lock[self.sysnum-1] = True
            # time.sleep(0.05)

        self.pump_on(self.P_waste_pins)
        time.sleep(self.P_waste_times)
        self.pump_off(self.P_waste_pins)

        self.pump_on(self.P_nut_pins)
        time.sleep(self.P_nut_times)
        self.pump_off(self.P_nut_pins)

        diff = self.od_thr - self.avOD
        increase = self.avOD - self.last_dilutionOD

        try:
            if self.pipins:
                GPIO.setmode(GPIO.BCM)
                for pin in self.pin_list:
                    GPIO.setup(pin, GPIO.OUT)
            else:
                self.pins = [None]*(max(self.pin_list)+1)
                self.mcp = self.gpioe[self.gpio_add.index(self.config[self.sysstr].getint('m_address'))]

                for pin in self.pin_list:
                    self.pins[pin] = self.mcp.get_pin(pin)
                    self.pins[pin].direction = digitalio.Direction.OUTPUT
                    self.pins[pin].value = False


            #bellow od threshold
            if self.avOD < self.od_thr:
                #decreasing
                if self.avOD <= self.last_dilutionOD:

                    if increase < self.last_increase and self.last_increase < 0:
                        self.time_between_pumps = 0.7*self.time_between_pumps

                        brmsg = self.slack_client.api_call(
                            "chat.postMessage",
                            channel = self.chan,
                            username = self.sysstr,
                            icon_url = self.slack_usericon,
                            thread_ts = self.threadts,
                            text = "OD = %0.3f and decreasing rapidly. Reducing time between pumps to %f" % (self.avOD, self.p_nut_times)
                            )

                    else:
                        self.time_between_pumps = 0.9*self.time_between_pumps

                        brmsg = self.slack_client.api_call(
                            "chat.postMessage",
                            channel = self.chan,
                            username = self.sysstr,
                            icon_url = self.slack_usericon,
                            thread_ts = self.threadts,
                            text = "OD = %0.3f and decreasing. Reducing time between pumps to %f" % (self.avOD, self.p_nut_times)
                            )
                #increasing
                elif self.avOD > self.last_dilutionOD:
                    #diff greater than increase
                    if diff >= increase:

                        bismsg = self.slack_client.api_call(
                            "chat.postMessage",
                            channel = self.chan,
                            username = self.sysstr,
                            icon_url = self.slack_usericon,
                            thread_ts = self.threadts,
                            text = "OD = %0.3f and increasing. Time between pumps is %f" % (self.avOD, self.p_nut_times)
                            )
                    #diff less than increase
                    elif:
                        self.time_between_pumps = 1.1*self.time_between_pumps

                        birmsg = self.slack_client.api_call(
                            "chat.postMessage",
                            channel = self.chan,
                            username = self.sysstr,
                            icon_url = self.slack_usericon,
                            thread_ts = self.threadts,
                            text = "OD = %0.3f and increasing rapidly. Increasing time between pumps to %f" % (self.avOD, self.p_nut_times)
                            )

            #above od threshold
            elif self.avOD >= self.od_thr:
                #increasing
                if self.avOD >= self.last_dilutionOD:

                    if increase > self.last_increase and self.last_increase > 0:

                        self.time_between_pumps = 1.3*self.time_between_pumps

                        airmsg = self.slack_client.api_call(
                            "chat.postMessage",
                            channel = self.chan,
                            username = self.sysstr,
                            icon_url = self.slack_usericon,
                            thread_ts = self.threadts,
                            text = "OD = %0.3f and increasing rapidly. Increasing time between pumps to %f" % (self.avOD, self.p_nut_times)
                            )

                    else:
                        self.time_between_pumps = 1.1*self.time_between_pumps

                        aismsg = self.slack_client.api_call(
                            "chat.postMessage",
                            channel = self.chan,
                            username = self.sysstr,
                            icon_url = self.slack_usericon,
                            thread_ts = self.threadts,
                            text = "OD = %0.3f and increasing slowly. Increasing time between pumps to %f" % (self.avOD, self.p_nut_times)
                            )

                #decreasing
                elif self.avOD < self.last_dilutionOD
                    #diff greater than decrease
                    if increase > diff:

                        arsmsg = self.slack_client.api_call(
                            "chat.postMessage",
                            channel = self.chan,
                            username = self.sysstr,
                            icon_url = self.slack_usericon,
                            thread_ts = self.threadts,
                            text = "OD = %0.3f and decreasing. Time between pumps is %f" % (self.avOD, self.p_nut_times)
                            )

                    elif increase >= diff:

                        arrmsg = self.slack_client.api_call(
                            "chat.postMessage",
                            channel = self.chan,
                            username = self.sysstr,
                            icon_url = self.slack_usericon,
                            thread_ts = self.threadts,
                            text = "OD = %0.3f and decreasing rapidly. Time between pumps is %f" % (self.avOD, self.p_nut_times)
                            )

#            if self.avOD > self.OD_min:
#                self.pump_on(self.P_waste_pins)
#                time.sleep(self.P_waste_times)
#                self.pump_off(self.P_waste_pins)
#
#                self.waste = 3
#                self.vial_drug_mass = self.vial_drug_mass - (self.vial_drug_mass/12)
#
#                if self.avOD > self.OD_thr and self.avOD > self.last_dilutionOD:
#                    print('[%s] OD Threshold exceeded, pumping %s' % (self.sysstr,self.drug_name))
#
#                    self.pump_on(self.P_drug_pins)
#                    time.sleep(self.P_drug_times)
#                    self.pump_off(self.P_drug_pins)
#                    self.drug = 2
#
#                    self.vial_drug_mass = self.vial_drug_mass + self.drug_conc*self.drug_vol
#
#                    drugamsg = self.slack_client.api_call(
#                        "chat.postMessage",
#                        channel = self.chan,
#                        username=self.sysstr,
#                        icon_url = self.slack_usericon,
#                        thread_ts = self.threadts,
#                        text = "OD = %0.3f, pumping %s. Drug concentration: %f ug/mL" % (self.avOD, self.drug_name, (self.vial_drug_mass)/12)
#                        )
#
#
#                else:
#                    print('[%s] OD below threshold, pumping nutrient' % self.sysstr)
#
#                    self.pump_on(self.P_nut_pins)
#                    time.sleep(self.P_nut_times)
#                    self.pump_off(self.P_nut_pins)
#                    self.nut = 1
#
#                    thramgs = self.slack_client.api_call(
#                        "chat.postMessage",
#                        channel = self.chan,
#                        username=self.sysstr,
#                        icon_url = self.slack_usericon,
#                        thread_ts = self.threadts,
#                        text = "OD = %0.3f, pumping nutrient. %s concentration: %f ug/mL" % (self.avOD, self.drug_name.capitalize(), (self.vial_drug_mass)/12)
#                        )
#
#
#            else: #report even when pumps aren't activated yet
#
#                # self.vial_drug_mass = 0 if self.vial_drug_mass < 0
#
#                thrbmsg = self.slack_client.api_call(
#                        "chat.postMessage",
#                        channel = self.chan,
#                        username=self.sysstr,
#                        icon_url = self.slack_usericon,
#                        thread_ts = self.threadts,
#                        text = "OD = %0.3f, OD below nutrient pump threshold." % (self.avOD)
#                        )
        except Exception as e:
            print ('[%s] CA - WARNING ADC REQUEST CRASHED' % self.sysstr)
            print(e)
            pass

        self.last_dilutionOD = self.avOD

        self.last_increase = increase
        # i2c_lock[self.sysnum-1] = False
        # i2c_q.pop(0)

        self.thread_locks['control_alg'].release()

    def secondsToText(self,secs):
        if secs:
            days = secs//86400
            hours = (secs - days*86400)//3600
            minutes = (secs - days*86400 - hours*3600)//60
            seconds = secs - days*86400 - hours*3600 - minutes*60
            result = ("{0} day{1}, ".format(days, "s" if days!=1 else "") if days else "") + \
            ("{0} hour{1}, ".format(hours, "s" if hours!=1 else "") if hours else "") + \
            ("{0} minute{1}, ".format(minutes, "s" if minutes!=1 else "") if minutes else "") + \
            ("{0} second{1}, ".format(seconds, "s" if seconds!=1 else "") if seconds else "")
            return result[:-2]
        else:
            return "0 seconds"

    def on_timer(self):
        self.loops += 1
        if self.loops < self.total_time/self.time_between_ODs:
            threading.Timer(self.time_between_ODs,self.on_timer).start()
        else:
            self.now = datetime.now()
            self.nows = time.time()
            print('[%s] Experiment Complete at %02s:%02s:%02s ' % (self.sysstr, self.now.hour, self.now.minute, self.now.second))
            # GPIO.output(P_fan_pins,0)

            compmsg = self.slack_client.api_call(
                "chat.postMessage",
                channel = self.chan,
                username=self.sysstr,
                icon_url = self.slack_usericon,
                thread_ts = self.threadts,
                text = "Experiment Complete at %02s:%02s:%02s " % (self.now.hour, self.now.minute, self.now.second)
                )

        if self.loops > 1:
            if not self.thread_locks['threads'].locked():
                self.threads['threads'] = threading.Thread(target=self.thread_split())
                self.threads['threads'].start()
        else:
            self.threads['threads'] = threading.Thread(target=self.thread_split())
            self.threads['threads'].start()

    def thread_split(self):
        self.thread_locks['threads'].acquire()
        self.now = datetime.now()
        self.nows = time.time()


        #print(self.loops)
        self.elapsed_time = self.now - self.start_time
        self.active_threads = threading.active_count()

        # Count see if the thread is locked for a long time


        global i2c_q
        global graph_q

        if self.loops > 1:
            if not self.thread_locks['adc'].locked():
                self.thread_locks['adc'].acquire()
                i2c_q.append(str(self.sysnum-1)+'OD')

            if not self.thread_locks['dynL'].locked():
                if (self.loops % int(self.thresh_check*60/self.time_between_ODs)) == 0 and not self.OD_thr_set:
                    self.threads['dynL'] = threading.Thread(target=self.dynLimit)
                    self.threads['dynL'].start()

            if not self.thread_locks['control_alg'].locked():
                if self.loops % (self.loops_between_pumps) == 0:
                    self.thread_locks['control_alg'].acquire()
                    i2c_q.append(str(self.sysnum-1)+'CA')

            if not self.thread_locks['graphs'].locked():
                if (self.loops % int(self.time_between_graphs*60/self.time_between_ODs)) == 0:
                    self.thread_locks['graphs'].acquire()
                    graph_q.append(self.sysnum-1)
        else:
            self.thread_locks['adc'].acquire()
            i2c_q.append(str(self.sysnum-1)+'OD')

            if (self.loops % int(self.thresh_check*60/self.time_between_ODs)) == 0 and not self.OD_thr_set:
                self.threads['dynL'] = threading.Thread(target=self.dynLimit)
                self.threads['dynL'].start()

            if self.loops % (self.loops_between_pumps) == 0:
                self.thread_locks['control_alg'].acquire()
                i2c_q.append(str(self.sysnum-1)+'CA')

            if (self.loops % int(self.time_between_graphs*60/self.time_between_ODs)) == 0:
                self.thread_locks['graphs'].acquire()
                graph_q.append(self.sysnum-1)


        # save the data to disk if it's time
        if (self.loops % int(self.time_between_saves*60/self.time_between_ODs)) == 0:
            if self.printing:
                print('[%s] Saving to disk' % self.sysstr)
            self.threads['save'] = threading.Thread(target=self.savefunc)
            self.threads['save'].start()
        else:
            if self.printing:
                print('[%s] Buffering Data' % self.sysstr)
            self.threads['buffer'] = threading.Thread(target=self.bufferdata)
            self.threads['buffer'].start()


        if self.printing:
            print ('[%s] Elapsed Time: %s ; Threads = %d ; OD = %.3f' % (self.sysstr, self.secondsToText(int(self.elapsed_time.total_seconds())),self.active_threads,self.currOD))

        self.thread_locks['threads'].release()

chips = IC_init()

threading.Thread(target = i2c_controller).start()

threading.Thread(target = graph_controller).start()

if config['MAIN'].getboolean('temp_sensor'): threading.Thread(target = temp_sensor_func).start()

eve_starter()

threading.Thread(target = live_plotter).start()


# threading.Thread(target = slackresponder).start()
