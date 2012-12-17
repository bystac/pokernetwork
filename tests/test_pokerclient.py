#!/usr/bin/env python
# -*- py-indent-offset: 4; coding: utf-8; mode: python -*-
#
# Copyright (C) 2007, 2008, 2009 Loic Dachary <loic@dachary.org>
# Copyright (C)       2008 Bradley M. Kuhn <bkuhn@ebb.org>
# Copyright (C) 2006 Mekensleep <licensing@mekensleep.com>
#
# This software's license gives you freedom; you can copy, convey,
# propagate, redistribute and/or modify this program under the terms of
# the GNU Affero General Public License (AGPL) as published by the Free
# Software Foundation (FSF), either version 3 of the License, or (at your
# option) any later version of the AGPL published by the FSF.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program in a file in the toplevel directory called
# "AGPLv3".  If not, see <http://www.gnu.org/licenses/>.
#
# Authors:
#  Loic Dachary <loic@dachary.org>
#  Bradley M. Kuhn <bkuhn@ebb.org>

import unittest, sys, os
import tempfile
import shutil

from os import path

TESTS_PATH = path.dirname(path.realpath(__file__))
sys.path.insert(0, path.join(TESTS_PATH, ".."))

from config import config
import libxml2

import socket
import time
#
# Must be done before importing pokerclient or pokerclient
# will have to be patched too.
#
from tests import testclock

from twisted.trial import unittest, runner, reporter
import twisted.internet.base
from twisted.internet import reactor, defer, error, base
from twisted.python import failure, runtime

from pokerengine import pokertournament

from log_history import log_history

twisted.internet.base.DelayedCall.debug = False

from pokernetwork import pokerservice
from pokernetwork import pokernetworkconfig
from pokernetwork import pokerclient
from pokernetwork import currencyclient
currencyclient.CurrencyClient = currencyclient.FakeCurrencyClient

from pokerpackets.clientpackets import *

from mock_transport import PairedDeferredTransport

class ConstantDeckShuffler:
    def shuffle(self, what):
        what[:] = [40, 13, 32, 9, 19, 31, 15, 14, 50, 34, 20, 6, 43, 44, 28, 29, 48, 3, 21, 45, 23, 37, 35, 11, 5, 22, 24, 30, 27, 39, 46, 33, 0, 8, 1, 42, 36, 16, 49, 2, 10, 26, 4, 18, 7, 41, 47, 17]

from pokerengine import pokergame
pokergame.shuffler = ConstantDeckShuffler()

class ConstantPlayerShuffler:
    def shuffle(self, what):
        what.sort()

pokertournament.shuffler = ConstantPlayerShuffler()

settings_xml_server = """<?xml version="1.0" encoding="UTF-8"?>
<server verbose="6" ping="300000" autodeal="yes" simultaneous="4" chat="yes" auto_create_account="yes" >
  <delays autodeal="20" round="0" position="0" showdown="0" autodeal_max="1" finish="0" messages="60" />

  <table name="Table1" variant="holdem" betting_structure="100-200_2000-20000_no-limit" seats="10" player_timeout="60" currency_serial="1" />
  <table name="Table2" variant="holdem" betting_structure="100-200_2000-20000_no-limit" seats="10" player_timeout="60" currency_serial="1" />
  <table name="Table3" variant="holdem" betting_structure="test18pokerclient" seats="10" player_timeout="600" muck_timeout="600" currency_serial="1" forced_dealer_seat="0" />

  <listen tcp="19480" />

  <cashier acquire_timeout="5" pokerlock_queue_timeout="30" user_create="yes" />
  <database
    host="%(dbhost)s" name="%(dbname)s"
    user="%(dbuser)s" password="%(dbuser_password)s"
    root_user="%(dbroot)s" root_password="%(dbroot_password)s"
    schema="%(tests_path)s/../database/schema.sql"
    command="%(mysql_command)s" />
  <path>%(engine_path)s/conf %(tests_path)s/conf</path>
  <users temporary="BOT.*"/>
</server>
""" % {
    'dbhost': config.test.mysql.host,
    'dbname': config.test.mysql.database,
    'dbuser': config.test.mysql.user.name,
    'dbuser_password': config.test.mysql.user.password,
    'dbroot': config.test.mysql.root_user.name,
    'dbroot_password': config.test.mysql.root_user.password,
    'tests_path': TESTS_PATH,
    'engine_path': config.test.engine_path,
    'mysql_command': config.test.mysql.command
}

settings_xml_client = """<?xml version="1.0" encoding="UTF-8"?>
<settings display2d="yes" display3d="no" ping="15000" verbose="6" delays="true" tcptimeout="2000" upgrades="no">
   <delays blind_ante_position="0" position="0" begin_round="0" end_round="0" end_round_last="0" showdown="0" lag="60"/> 
  <screen fullscreen="no" width="1024" height="768"/>
  <name>user1</name>
  <passwd>password1</passwd>
  <remember>yes</remember>
  <muck>yes</muck>
  <auto_post>no</auto_post>
  <chat max_chars="40" line_length="20"/>
  <web browser="/usr/bin/firefox">http://localhost/poker-web/</web>
  <sound>yes</sound>
  <tournaments currency_serial="1" type="sit_n_go" sort="name"/>
  <lobby currency_serial="1" type="holdem" sort="name"/>
  <shadow>yes</shadow>
  <vprogram>yes</vprogram>
  
  <path>%(engine_path)s/conf %(tests_path)s/conf</path>
  <data path="data" sounds="data/sounds"/>
  <handlist start="0" count="10"/>
</settings>
""" % {
    'dbhost': config.test.mysql.host,
    'dbname': config.test.mysql.database,
    'dbuser': config.test.mysql.user.name,
    'dbuser_password': config.test.mysql.user.password,
    'dbroot': config.test.mysql.root_user.name,
    'dbroot_password': config.test.mysql.root_user.password,
    'tests_path': TESTS_PATH,
    'engine_path': config.test.engine_path,
    'mysql_command': config.test.mysql.command
}

TABLE1 = 1
TABLE2 = 2
TABLE3 = 3

class PokerClientTestCase(unittest.TestCase):

    timeout = 500
    
    def destroyDb(self):
        if len(config.test.mysql.root_user.password) > 0:
            os.system("%(mysql_command)s -u %(dbroot)s --password='%(dbroot_password)s' -h '%(dbhost)s' -e 'DROP DATABASE IF EXISTS %(dbname)s'" % {
                'mysql_command': config.test.mysql.command,
                'dbroot': config.test.mysql.root_user.name,
                'dbroot_password': config.test.mysql.root_user.password,
                'dbhost': config.test.mysql.host,
                'dbname': config.test.mysql.database
            })
        else:
            os.system("%(mysql_command)s -u %(dbroot)s -h '%(dbhost)s' -e 'DROP DATABASE IF EXISTS %(dbname)s'" % {
                'mysql_command': config.test.mysql.command,
                'dbroot': config.test.mysql.root_user.name,
                'dbhost': config.test.mysql.host,
                'dbname': config.test.mysql.database
            })

    def createTourneysSchedules(self):
        stmts = []
        stmts.append(
            'INSERT INTO `tourneys_schedule` (`name`, `description_short`, `description_long`, `players_quota`, `variant`, `betting_structure`, `seats_per_game`, `currency_serial`, `buy_in`, `rake`, `sit_n_go`, `start_time`, `register_time`, `respawn`, `respawn_interval`) ' \
            'VALUES ("sitngo2", "Sit and Go 2 players, Holdem", "Sit and Go 2 players", "2", "holdem", "level-15-30-no-limit", "2", 1, "300000", "0", "y", "0", "0", "y", "0");'
        )
        stmts.append(
            'INSERT INTO `tourneys_schedule` (`name`, `description_short`, `description_long`, `players_quota`, `variant`, `betting_structure`, `seats_per_game`, `currency_serial`, `buy_in`, `rake`, `sit_n_go`, `breaks_interval`, `rebuy_delay`, `add_on`, `add_on_delay`, `start_time`, `register_time`, `respawn`, `respawn_interval`, `players_min`) ' \
            'VALUES ("regular1", "Holdem No Limit Freeroll", "Holdem No Limit Freeroll", "1000", "holdem", "level-001", "10", 1, "0", "0", "n", "60", "30", "1", "60", unix_timestamp(now() + INTERVAL 2 MINUTE), unix_timestamp(now() - INTERVAL 1 HOUR), "n", "0", 3);'
        )
        
        if len(config.test.mysql.root_user.password) > 0:
            prefix = "%(mysql_command)s -u %(dbroot)s --password='%(dbroot_password)s' -h '%(dbhost)s' -D '%(dbname)s' -e '%%s'" % {
                'mysql_command': config.test.mysql.command,
                'dbroot': config.test.mysql.root_user.name,
                'dbroot_password': config.test.mysql.root_user.password,
                'dbhost': config.test.mysql.host,
                'dbname': config.test.mysql.database
            }
        else:
            prefix = "%(mysql_command)s -u %(dbroot)s -h '%(dbhost)s' -D '%(dbname)s' -e '%%s'" % {
                'mysql_command': config.test.mysql.command,
                'dbroot': config.test.mysql.root_user.name,
                'dbhost': config.test.mysql.host,
                'dbname': config.test.mysql.database
            }
        for stmt in stmts:
            os.system(prefix % stmt)

    def setUpConnection(self, serial):
        server_protocol = self.server_protocol[serial] = self.server_factory.buildProtocol(('127.0.0.1',0))
        client_protocol = self.client_protocol[serial] = self.client_factory[serial].buildProtocol(('127.0.0.1',0))
        server_transport = PairedDeferredTransport(protocol=server_protocol, foreignProtocol=client_protocol)
        client_transport = PairedDeferredTransport(protocol=client_protocol, foreignProtocol=server_protocol)
        server_protocol.makeConnection(server_transport)
        client_protocol.makeConnection(client_transport)
    
    def setUpServer(self):
        settings = pokernetworkconfig.Config([])
        settings.loadFromString(settings_xml_server)
        #
        # Setup server
        #
        self.service = pokerservice.PokerService(settings)
        self.service.startService()
        self.createTourneysSchedules()
        self.service.updateTourneysSchedule()
        self.server_factory = pokerservice.IPokerFactory(self.service)

    def setUpClient(self, serial):
        settings = pokernetworkconfig.Config([])
        settings.loadFromString(settings_xml_client)
        log_history.reset()
        self.client_factory[serial] = pokerclient.PokerClientFactory(settings = settings)
        self.assertEquals(log_history.get_all(), ["delays {'lag': 60.0, 'end_round_last': 0.0, 'showdown': 0.0, 'blind_ante_position': 0.0, 'position': 0.0, 'begin_round': 0.0, 'end_round': 0.0}"])
        log_history.reset()

        def setUpProtocol(client_protocol):
            client_protocol._poll_frequency = 0.1
            return client_protocol
        d = self.client_factory[serial].established_deferred
        d.addCallback(setUpProtocol)
        return d

    # ------------------------------------------------------
    def setUp(self):
        log_history.reset()
        testclock._seconds_reset()
        self.destroyDb()
        self.setUpServer()
        self.client_factory = [None, None]
        self.client_protocol = [None, None]
        self.server_protocol = [None, None]
        
        def connectClient(serial):
            self.setUpConnection(serial)
        
        # really connect the second client. the first is about to connect
        d0 = self.setUpClient(0)
        
        d1 = self.setUpClient(1)
        d1.addCallback(lambda x: connectClient(0))
        connectClient(1)
        return d1

    def cleanSessions(self, arg):
        #
        # twisted Session code has leftovers : disable the hanging delayed call warnings
        # of trial by nuking all what's left.
        #
        pending = reactor.getDelayedCalls()
        if pending:
            for p in pending:
                if p.active():
#                    print "still pending:" + str(p)
                    p.cancel()
        return arg

    def tearDownClearMessages(arg1, arg2):
        log_history.reset()
        return (arg1, arg2)

    def tearDown(self):
        d = self.service.stopService()
        d.addCallback(lambda x: self.destroyDb())
        d.addCallback(self.cleanSessions)
        return d

    def quit(self, args):
        client = args[0]
        client.sendPacket(PacketQuit())
        if hasattr(client, "transport"):
            client.transport.loseConnection()
            return client.connection_lost_deferred
        else:
            raise UserWarning, "quit does not have transport %d" % client.getSerial()
        
    def ping(self, client):
        self.assertEquals(log_history.search('protocol established') > 0, True)
        log_history.reset()
        client.sendPacket(PacketPing())
        self.assertEquals(log_history.get_all(), ['sendPacket: PING  type = 5 length = 3'])
        log_history.reset()
        return (client,)

    def test01_ping(self):
        """ test01_ping """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.ping)
        d.addCallback(self.quit)
        log_history.reset()
        return d

    def login(self, client, index):
        client.sendPacket(PacketPokerSetRole(roles = PacketPokerRoles.PLAY))
        client.sendPacket(PacketLogin(name = 'user%d' % index, password = 'password1'))
        d = client.packetDeferred(True, PACKET_POKER_PLAYER_INFO)
        return d

    def test02_login(self):
        """ test02_login """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.quit)
        return d

    def cashIn(self, client, url, value):
        note = self.service.cashier.currency_client._buildNote(url, value)
        (url, bserial, name, value) = note
        client.sendPacket(PacketPokerCashIn(
            serial = client.getSerial(),
            url = url,
            bserial = bserial,
            name = name,
            value = value
        ))
        client.setPrefix("[Client %d]" % client.getSerial())
        return client.packetDeferred(True, PACKET_ACK)

    def check_cashIn(self, (client, packet)):
        client.sendPacket(PacketPokerGetUserInfo(serial = client.getSerial()))
        d = client.packetDeferred(True, PACKET_POKER_USER_INFO)
        def validate((client, packet),):
            self.assertEquals(PACKET_POKER_USER_INFO, packet.type)
            self.assertEquals(2, len(packet.money))
            self.assertEquals([1, 2], packet.money.keys())
            self.assertEquals(100, packet.money[1][0])
            self.assertEquals(200, packet.money[2][0])
            return (client, packet)
        d.addCallback(validate)
        return d
                                   
    def test03_cashIn(self):
        """ test03_cashIn """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(lambda (client, packet): self.cashIn(client, "ONE", 100))
        d.addCallback(lambda (client, packet): self.cashIn(client, "TWO", 200))
        d.addCallback(self.check_cashIn)
        d.addCallback(self.quit)
        return d

    def sit(self, (client, packet), game_id = TABLE1, seat = -1, auto_muck = pokergame.AUTO_MUCK_ALWAYS):
        client.sendPacket(PacketPokerTableJoin(
            serial = client.getSerial(), 
            game_id = game_id
        ))
        client.sendPacket(PacketPokerSeat(
            serial = client.getSerial(), 
            game_id = game_id, 
            seat = seat
        ))
        client.sendPacket(PacketPokerAutoBlindAnte(
            serial = client.getSerial(), 
            game_id = game_id
        ))
        client.sendPacket(PacketPokerBuyIn(
            serial = client.getSerial(), 
            game_id = game_id, amount = 200000
        ))
        if auto_muck != pokergame.AUTO_MUCK_ALWAYS:
            client.sendPacket(PacketPokerAutoMuck(serial = client.getSerial(), game_id = game_id, auto_muck = auto_muck))
        client.sendPacket(PacketPokerSit(
            serial = client.getSerial(),
            game_id = game_id
        ))
        return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)
        
    def allIn(self, (client, packet), sit_out = True):
        self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
        self.assertEqual(client.getSerial(), packet.serial)
        if sit_out:
            client.sendPacket(PacketPokerSitOut(
                serial = packet.serial,
                game_id = packet.game_id
            ))
        game = client.getGame(packet.game_id)
        player = game.getPlayer(packet.serial)
        if game.canRaise(player.serial):
            client.sendPacket(PacketPokerRaise(
                serial = packet.serial,
                game_id = packet.game_id,
                amount = player.money
            ))
        else:
            client.sendPacket(PacketPokerCall(
                serial = packet.serial,
                game_id = packet.game_id
            ))

        return client.packetDeferred(True, PACKET_POKER_WIN)
        
    def check_or_call(self, (client, packet)):
        self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
        self.assertEqual(client.getSerial(), packet.serial)
        game = client.getGame(packet.game_id)
        player = game.getPlayer(packet.serial)
        if game.canCheck(player.serial):
            client.sendPacket(PacketPokerCheck(
                serial = packet.serial,
                game_id = packet.game_id
            ))
        else:
            client.sendPacket(PacketPokerCall(
                serial = packet.serial,
                game_id = packet.game_id
            ))

        return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)
        
    def win(self, (client, packet), expect = None):
        if expect:
            return client.packetDeferred(True, expect)
        else:
            return (client, packet)
        
    def test04_playHand(self):
        """ test04_playHand """
        d = [None, None]
        for index in (0,1):
            d[index] = self.client_factory[index].established_deferred or defer.succeed(self.client_protocol[index])
            d[index].addCallback(self.login, index)
            d[index].addCallback(lambda (client, packet): self.cashIn(client, "ONE", 200000))
            d[index].addCallback(self.sit)
            d[index].addCallback(self.allIn)
            d[index].addCallback(self.win)
            d[index].addCallback(self.quit)
        return defer.DeferredList(d)
    
    def getUserInfo(self, (client, packet)):
        game = client.getGame(packet.game_id)
        self.assertEqual(False, game.isRunning())
        client.sendPacket(PacketPokerGetUserInfo(serial = client.getSerial()))
        log_history.reset()
        return client.packetDeferred(True, PACKET_POKER_USER_INFO)

    def printUserInfo(self, (client, packet)):
        self.assertEquals(log_history.search("handleUserInfo: type = POKER_USER_INFO(92) serial = %d name = user%d, password = , email = , rating = 1000, affiliate = 0" % (packet.serial, packet.serial - 4)) >= 0, True)
        self.assertEqual(PACKET_POKER_USER_INFO, packet.type)
        return (client, packet)
        
    def test05_userInfo(self):
        """ test05_userInfo """
        d = [None, None]
        for index in (0,1):
            d[index] = self.client_factory[index].established_deferred or defer.succeed(self.client_protocol[index])
            d[index].addCallback(self.login, index)
            d[index].addCallback(lambda (client, packet): self.cashIn(client, "ONE", 200000))
            d[index].addCallback(self.sit)
            d[index].addCallback(self.allIn)
            d[index].addCallback(self.getUserInfo)
            d[index].addCallback(self.printUserInfo)
            d[index].addCallback(self.quit)
        return defer.DeferredList(d)
    
    def test06_cannotGetSeat(self):
        """ test06_cannotGetSeat """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(lambda (client, packet): self.cashIn(client, "ONE", 200000))
        def wrongSeat((client, packet),):
            client.sendPacket(PacketPokerTableJoin(
                serial = client.getSerial(),
                game_id = TABLE1
            ))
            client.sendPacket(PacketPokerSeat(
                serial = client.getSerial(),
                game_id = TABLE1,
                seat = 42
            ))
            return client.packetDeferred(True, PACKET_POKER_SEAT)
        d.addCallback(wrongSeat)
        def checkWrongSeat((client, packet),):
            self.assertEqual(PACKET_POKER_SEAT, packet.type)
            self.assertEqual(-1, packet.seat)
            return (client, packet)
        d.addCallback(checkWrongSeat)
        d.addCallback(self.quit)
        return d

    def rebuy(self, (client, packet)):
        client.sendPacket(PacketPokerTableJoin(
            serial = client.getSerial(),
            game_id = TABLE1
        ))
        client.sendPacket(PacketPokerSeat(
            serial = client.getSerial(),
            game_id = TABLE1
        ))
        client.sendPacket(PacketPokerBuyIn(
            serial = client.getSerial(),
            game_id = TABLE1,
            amount = 200000
        ))
        client.sendPacket(PacketPokerRebuy(
            serial = client.getSerial(),
            game_id = TABLE1,
            amount = 200000
        ))
        return client.packetDeferred(True, PACKET_POKER_PLAYER_CHIPS)

    def test07_rebuy(self):
        """ test07_rebuy """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(lambda (client, packet): self.cashIn(client, "ONE", 400000))
        d.addCallback(self.rebuy)
        def checkSit((client, packet),):
            self.assertEqual(PACKET_POKER_PLAYER_CHIPS, packet.type)
            self.assertEqual(0, packet.money)
            return client.packetDeferred(True, PACKET_POKER_PLAYER_CHIPS)
        d.addCallback(checkSit)
        def checkBuyIn((client, packet),):
            self.assertEqual(PACKET_POKER_PLAYER_CHIPS, packet.type)
            self.assertEqual(200000, packet.money)
            return client.packetDeferred(True, PACKET_POKER_PLAYER_CHIPS)
        d.addCallback(checkBuyIn)
        def checkRebuy((client, packet),):
            self.assertEqual(PACKET_POKER_PLAYER_CHIPS, packet.type)
            self.assertEqual(400000, packet.money)
            return (client, packet)
        d.addCallback(checkRebuy)
        d.addCallback(self.quit)
        return d

    def processingHand(self, (client, packet),):
        self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
        client.sendPacket(PacketPokerProcessingHand(
            serial = client.getSerial(),
            game_id = TABLE1
        ))
        return (client, packet)

    def readyToPlay(self, (client, packet),):
        client.sendPacket(PacketPokerReadyToPlay(
            serial = client.getSerial(),
            game_id = TABLE1
        ))
        return (client, packet)

    def test08_processing_readytoplay(self):
        """ test08_processing_readytoplay """
        d = [None, None]
        for index in (0,1):
            d[index] = self.client_factory[index].established_deferred or defer.succeed(self.client_protocol[index])
            d[index].addCallback(self.login, index)
            d[index].addCallback(lambda (client, packet): self.cashIn(client, "ONE", 600000))
            d[index].addCallback(self.sit)
            d[index].addCallback(self.processingHand)
            d[index].addCallback(self.allIn)
            def atShowdown((client, packet),):
                client.sendPacket(PacketPokerRebuy(
                    serial = client.getSerial(),
                    game_id = TABLE1,
                    amount = 200000
                ))
                client.sendPacket(PacketPokerSit(
                    serial = client.getSerial(),
                    game_id = TABLE1
                ))
                return (client, packet)
            d[index].addCallback(atShowdown)
            if index == 1:
                d[index].addCallback(self.readyToPlay)
            def nextTurn((client, packet),):
                return client.packetDeferred(True, PACKET_POKER_START)
            d[index].addCallback(nextTurn)
            #
            # Game will start after the client index=0 times out 
            # and is marked as bugous because it failed to send
            # the READY_TO_PLAY packet
            #
            if index == 0:
                def checkBugousClient((client, packet),):
                    table = self.service.tables.values()[0]
                    has_bugous = False
                    for server_client in table.avatar_collection.get(client.getSerial()):
                        if server_client.bugous_processing_hand == True:
                            has_bugous = True
                    self.assertEqual(True, has_bugous, "has bugous")
                    return (client, packet)
                d[index].addCallback(checkBugousClient)
            d[index].addCallback(self.quit)
        return defer.DeferredList(d)
    
    def test09_serverShutdown(self):
        """ test04_serverShutdown : the clients are still seated """
        d = [None, None]
        for index in (0,1):
            d[index] = self.client_factory[index].established_deferred or defer.succeed(self.client_protocol[index])
            d[index].addCallback(self.login, index)
            d[index].addCallback(lambda (client, packet): self.cashIn(client, "ONE", 200000))
            d[index].addCallback(self.sit)
            d[index].addCallback(self.allIn)
        return defer.DeferredList(d)
    
    def test10_playerImage(self):
        """ test10_login """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        def setPlayerImage((client, packet),):
            client.sendPacket(PacketPokerPlayerImage(
                serial = client.getSerial(),
                image = "2345"
            ))
            return client.packetDeferred(True, PACKET_ACK)
        d.addCallback(setPlayerImage)
        def getPlayerImage((client, packet),):
            self.assertEqual(PACKET_ACK, packet.type)
            client.sendPacket(PacketPokerGetPlayerImage(serial = client.getSerial()))
            return client.packetDeferred(True, PACKET_POKER_PLAYER_IMAGE)
        d.addCallback(getPlayerImage)
        def checkPlayerImage((client, packet),):
            self.assertEqual(PACKET_POKER_PLAYER_IMAGE, packet.type)
            self.assertEqual("2345", packet.image)
            return (client, packet)
        d.addCallback(checkPlayerImage)
        d.addCallback(self.quit)
        return d

    def cashOut(self, client, url, value):
        client.sendPacket(PacketPokerCashOut(serial = client.getSerial(), url = url, value = value))
        return client.packetDeferred(True, PACKET_POKER_CASH_OUT)

    def cashOutCommit(self, (client, packet)):
        self.assertEquals(PACKET_POKER_CASH_OUT, packet.type)
        client.sendPacket(PacketPokerCashOutCommit(
            serial = client.getSerial(), 
            transaction_id = packet.name
        ))
        return client.packetDeferred(True, PACKET_ACK)

    def test11_cashOut_zero(self):
        """ test11_cashOut_zero """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(lambda (client, packet): self.cashIn(client, "ONE", 100))
        d.addCallback(lambda (client, packet): self.cashIn(client, "TWO", 200))
        d.addCallback(self.check_cashIn)
        d.addCallback(lambda (client, packet): self.cashOut(client, "ONE", 100))
        d.addCallback(self.cashOutCommit)
        def check_cashOut((client, packet),):
            client.sendPacket(PacketPokerGetUserInfo(serial = client.getSerial()))
            d = client.packetDeferred(True, PACKET_POKER_USER_INFO)
            def validate((client, packet),):
                self.assertEquals(PACKET_POKER_USER_INFO, packet.type)
                self.assertEquals(2, len(packet.money))
                self.assertEquals([1, 2], packet.money.keys())
                self.assertEquals(0, packet.money[1][0])
                self.assertEquals(200, packet.money[2][0])
                return (client, packet)
            d.addCallback(validate)
            return d
        d.addCallback(check_cashOut)
        d.addCallback(self.quit)
        return d

    def test12_cashOut(self):
        """ test12_cashOut """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(lambda (client, packet): self.cashIn(client, "ONE", 100))
        d.addCallback(lambda (client, packet): self.cashIn(client, "TWO", 200))
        d.addCallback(self.check_cashIn)
        d.addCallback(lambda (client, packet): self.cashOut(client, "ONE", 50))
        d.addCallback(self.cashOutCommit)
        def check_cashOut((client, packet),):
            client.sendPacket(PacketPokerGetUserInfo(serial = client.getSerial()))
            d = client.packetDeferred(True, PACKET_POKER_USER_INFO)
            def validate((client, packet),):
                self.assertEquals(PACKET_POKER_USER_INFO, packet.type)
                self.assertEquals(2, len(packet.money))
                self.assertEquals([1, 2], packet.money.keys())
                self.assertEquals(50, packet.money[1][0])
                self.assertEquals(200, packet.money[2][0])
                return (client, packet)
            d.addCallback(validate)
            return d
        d.addCallback(check_cashOut)
        d.addCallback(self.quit)
        return d

    def test13_cashOut_failure(self):
        """ test12_cashOut """
        currencyclient.Verbose = True
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(lambda (client, packet): self.cashIn(client, "ONE", 100))
        d.addCallback(lambda (client, packet): self.cashIn(client, "TWO", 200))
        d.addCallback(self.check_cashIn)
        def cashOutFail((client, packet),):
            currencyclient.FakeCurrencyFailure = True
            client.sendPacket(PacketPokerCashOut(serial = client.getSerial(), url = "ONE", value = 50))
            client.sendPacket(PacketPokerGetUserInfo(serial = client.getSerial()))
            return client.packetDeferred(True, PACKET_POKER_USER_INFO)

        d.addCallback(cashOutFail)
        def check_cashOutFail((client, packet),):
            self.assertEquals(PACKET_POKER_USER_INFO, packet.type)
            self.assertEquals(2, len(packet.money))
            self.assertEquals([1, 2], packet.money.keys())
            self.assertEquals(100, packet.money[1][0])
            self.assertEquals(200, packet.money[2][0])
            return (client, packet)
        d.addCallback(check_cashOutFail)
        d.addCallback(self.quit)
        return d

    def test14_messages(self):
        d = self.client_factory[0].established_deferred
        def waitForMessage(client):
            db = self.service.db.db
            db.query("INSERT INTO messages (send_date, message) VALUES (" + str(testclock._seconds_value + 2) + ", 'the message')")
            return client.packetDeferred(True, PACKET_MESSAGE)
        d.addCallback(waitForMessage)
        return d

    def sitngo(self, (client, packet)):
        client.sendPacket(PacketPokerTableJoin(serial = client.getSerial(), game_id = TABLE1))
        client.sendPacket(PacketPokerSeat(serial = client.getSerial(), game_id = TABLE1))
        client.sendPacket(PacketPokerAutoBlindAnte(serial = client.getSerial(), game_id = TABLE1))
        client.sendPacket(PacketPokerBuyIn(
            serial = client.getSerial(),
            game_id = TABLE1,
            amount = 200000
        ))
        client.sendPacket(PacketPokerSit(serial = client.getSerial(), game_id = TABLE1))
        return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)
        
    def search_sitngo2(self, (client, packet),):
        client.sendPacket(PacketPokerTourneySelect(string = "sitngo2"))
        return client.packetDeferred(True, PACKET_POKER_TOURNEY_LIST)

    def register_sitngo2(self, (client, packet),):
        self.assertEqual(PACKET_POKER_TOURNEY_LIST, packet.type)
        self.assertEqual(1, len(packet.packets))
        tourney = packet.packets[0]
        client.sendPacket(PacketPokerTourneyRegister(serial = client.getSerial(), tourney_serial = tourney.serial))
        return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)

    def tourneyRank(self, (client, packet), ):
        self.assertEqual(PACKET_POKER_WIN, packet.type)
        return client.packetDeferred(True, PACKET_POKER_TOURNEY_RANK)

    # -----------------------------------------------
    def test15_0_playTourney(self):
        """ Play regular tourney, all players go allin immediately. Simplest case, no tricks. """
        d = [None, None]
        for index in (0,1):
            d[index] = self.client_factory[index].established_deferred or defer.succeed(self.client_protocol[index])
            d[index].addCallback(self.login, index)
            d[index].addCallback(lambda (client, packet): self.cashIn(client, "ONE", 200000000))

            d[index].addCallback(self.search_sitngo2)
            d[index].addCallback(self.register_sitngo2)
            d[index].addCallback(lambda arg: self.allIn(arg, sit_out = False))
            d[index].addCallback(self.tourneyRank)
            d[index].addCallback(self.quit)

        return defer.DeferredList(d)

    def check_breaks(self, (client, packet),):
        self.assertEqual(PACKET_POKER_TOURNEY_LIST, packet.type)
        self.assertEqual(1, len(packet.packets))
        tourney = packet.packets[0]
        self.failUnless(hasattr(tourney, 'breaks_first'))
        self.assertEqual(7200, tourney.breaks_first)
        self.failUnless(hasattr(tourney, 'breaks_interval'))
        self.assertEqual(3600, tourney.breaks_interval)
        self.failUnless(hasattr(tourney, 'breaks_duration'))
        self.assertEqual(300, tourney.breaks_duration)
        return (client,)

    # -----------------------------------------------
    def test15_1_check_breaks(self):
        """ """
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.search_sitngo2)
        d.addCallback(self.check_breaks)
        d.addCallback(self.quit)
        return d

    # -----------------------------------------------
    # FIXME fix this test
    def xtest16_playTourney_sitout_sit(self):
        """ Play regular tourney, one player sits out and sits back immediately afterwards. """
        d = [None, None]
        for index in (0,1):
            d[index] = self.client_factory[index].established_deferred or defer.succeed(self.client_protocol[index])
            d[index].addCallback(self.login, index)
            d[index].addCallback(lambda (client, packet): self.cashIn(client, "ONE", 200000000))

            d[index].addCallback(self.search_sitngo2)
            d[index].addCallback(self.register_sitngo2)
            if index == 0:
                def sitout((client, packet),):
                    self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
                    client.sendPacket(PacketPokerSitOut(
                        serial = packet.serial,
                        game_id = packet.game_id
                    ))
                    client.sendPacket(PacketPokerSit(
                        serial = packet.serial,
                        game_id = packet.game_id
                    ))
                    return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)
                d[index].addCallback(sitout)
            else:
                def callNraise((client, packet),):
                    self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
                    game = client.getGame(packet.game_id)
                    player = game.getPlayer(packet.serial)
                    client.sendPacket(PacketPokerRaise(
                        serial = packet.serial,
                        game_id = packet.game_id,
                        amount = player.money
                    ))
                    return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)
                d[index].addCallback(callNraise)
                    
            d[index].addCallback(self.check_or_call)
            d[index].addCallback(lambda arg: self.allIn(arg, sit_out = False))
            d[index].addCallback(self.tourneyRank)
            d[index].addCallback(self.quit)

        return defer.DeferredList(d)
    
    # -----------------------------------------------
    # FIXME fix this test
    def xtest17_playTourney_timeout_sit(self):
        """ Play regular tourney, one player timeouts out and sits back immediately afterwards. """
        self.service.settings.headerSet("/server/delays/@showdown", '20')
        for tourney in self.service.tourneys.values():
            tourney.player_timeout = 100
        d = [None, None]
        for index in (0,1):
            d[index] = self.client_factory[index].established_deferred or defer.succeed(self.client_protocol[index])
            d[index].addCallback(self.login, index)
            d[index].addCallback(lambda (client, packet): self.cashIn(client, "ONE", 200000000))

            d[index].addCallback(self.search_sitngo2)
            d[index].addCallback(self.register_sitngo2)
            if index == 0:
                def timeout((client, packet),):
                    self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
                    return client.packetDeferred(True, PACKET_POKER_TIMEOUT_NOTICE)
                d[index].addCallback(timeout)
                def sitback((client, packet),):
                    self.assertEqual(PACKET_POKER_TIMEOUT_NOTICE, packet.type)
                    client.sendPacket(PacketPokerSit(
                        serial = packet.serial,
                        game_id = packet.game_id
                    ))
                    return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)
                d[index].addCallback(sitback)
            else:
                def callNraise((client, packet),):
                    self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
                    game = client.getGame(packet.game_id)
                    player = game.getPlayer(packet.serial)
                    client.sendPacket(PacketPokerRaise(
                        serial = packet.serial,
                        game_id = packet.game_id,
                        amount = player.money
                    ))
                    return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)
                d[index].addCallback(callNraise)
                    
            d[index].addCallback(self.check_or_call)
            d[index].addCallback(lambda arg: self.allIn(arg, sit_out = False))
            d[index].addCallback(self.tourneyRank)
            d[index].addCallback(self.quit)

        return defer.DeferredList(d)
    xtest17_playTourney_timeout_sit.timeout = 50000

    def test18_blindAllIn(self):
        """ test18_blindAllIn """
        def raiseAlmostAllIn((client, packet)):
            self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
            self.assertEqual(client.getSerial(), packet.serial)
            game = client.getGame(packet.game_id)
            player = game.getPlayer(packet.serial)
            client.sendPacket(PacketPokerRaise(
                serial = packet.serial,
                game_id = packet.game_id,
                amount = player.money - 50
            ))
            return client.packetDeferred(True, PACKET_POKER_SELF_IN_POSITION)

        def fold((client, packet), expect = PACKET_POKER_SELF_IN_POSITION):
            self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
            self.assertEqual(client.getSerial(), packet.serial)
            game = client.getGame(packet.game_id)
            player = game.getPlayer(packet.serial)
            client.sendPacket(PacketPokerFold(
                serial = packet.serial,
                game_id = packet.game_id
            ))
            return client.packetDeferred(True, expect)

        def muck((client, packet),):
            self.assertEqual(client.getSerial(), packet.muckable_serials[0])
            game = client.getGame(packet.game_id)
            player = game.getPlayer(packet.serial)
            client.sendPacket(PacketPokerMuckAccept(
                serial = client.getSerial(),
                game_id = packet.game_id
            ))
            return client.packetDeferred(True, PACKET_POKER_WIN)

        def call((client, packet), expect = PACKET_POKER_SELF_IN_POSITION):
            self.assertEqual(PACKET_POKER_SELF_IN_POSITION, packet.type)
            self.assertEqual(client.getSerial(), packet.serial)
            game = client.getGame(packet.game_id)
            player = game.getPlayer(packet.serial)
            client.sendPacket(PacketPokerCall(
                serial = packet.serial,
                game_id = packet.game_id
            ))
            return client.packetDeferred(True, expect)

        d = [None, None]
        for index in (0,1):
            d[index] = self.client_factory[index].established_deferred or defer.succeed(self.client_protocol[index])
            d[index].addCallback(self.login, index)
            d[index].addCallback(lambda (client, packet): self.cashIn(client, "ONE", 200000))
            d[index].addCallback(self.sit, TABLE3, index, pokergame.AUTO_MUCK_NEVER)
            if index == 0: # serial 4
                d[index].addCallback(call, PACKET_POKER_MUCK_REQUEST)
                d[index].addCallback(muck)
            else:
                d[index].addCallback(raiseAlmostAllIn)
                d[index].addCallback(fold, PACKET_POKER_WIN)
            if index == 1: # serial 5
                d[index].addCallback(self.win, PACKET_POKER_MUCK_REQUEST)
                d[index].addCallback(muck)
            else:
                def waitWin((client, packet)):
                    return client.packetDeferred(True, PACKET_POKER_WIN)
                d[index].addCallback(waitWin)
            d[index].addCallback(self.quit)
        return defer.DeferredList(d)
    
    def test19_setPlayerDelay(self):
        d = self.client_factory[0].established_deferred
        def setPlayerDelay(client):

            class Player:
                def __init__(self):
                    self.user_data = {'delay': 0}

                def getUserData(self):
                    return self.user_data

            class Game:
                def __init__(self):
                    self.player = Player()

                def getPlayer(self, serial):
                    return self.player

            game = Game()
            client.setPlayerDelay(game, 42, 100)
            self.assertEqual(testclock._seconds_value + 100, game.player.user_data['delay'])
        d.addCallback(setPlayerDelay)
        return d

    def test20_resendPlayerTimeoutWarning(self):
        d = self.client_factory[0].established_deferred
        def resendPlayerTimeoutWarning(client):

            class Player:
                def __init__(self):
                    self.user_data = {'timeout': None }

                def getUserData(self):
                    return self.user_data

            class Game:
                def __init__(self):
                    self.player = Player()
                    self.id = 1010

                def getPlayer(self, serial):
                    return self.player

                def isRunning(self):
                    return True

                def getSerialInPosition(self):
                    return 0

            game = Game()
            client.setPlayerTimeout(game, PacketPokerTimeoutWarning(
                game_id = game.id,
                serial = client.getSerial(),
                timeout = 50
            ))

            self.assertEqual(int(testclock._seconds_value), game.player.user_data['timeout'][0])
            testclock._seconds_value += 1
            ( packet, ) = client.resendPlayerTimeoutWarning(game)
            self.assertEqual(int(testclock._seconds_value), packet.when)
            self.assertApproximates(50 - 1 - 1, packet.timeout, 1) # approximates because of rounding
            self.assertEqual(PACKET_POKER_TIMEOUT_WARNING, packet.type)
        d.addCallback(resendPlayerTimeoutWarning)
        return d
        
    def test21_publishDelay(self):
        d = self.client_factory[0].established_deferred
        def publishDelay(client):
            client.publishDelay(10)
            self.assertEqual(testclock._seconds_value + 10, client.publish_time)
        d.addCallback(publishDelay)
        return d

    def test22_resendPackets(self):
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(lambda (client, packet): self.cashIn(client, "ONE", 400000))
        d.addCallback(self.rebuy)
        def resendPackets((client, packet),):
            client.resendPackets(TABLE1)
            types = {}
            for packet in client.publish_packets:
                if packet.type == PACKET_POKER_TABLE:
                    types[packet.type] = 1
                elif packet.type == PACKET_POKER_BUY_IN_LIMITS:
                    for key in [ 'best', 'game_id', 'min', 'max' ]:
                        self.assert_(hasattr(packet, key))
                    types[packet.type] = 1

            self.assertEqual(types[PACKET_POKER_BUY_IN_LIMITS], 1)
            self.assertEqual(types[PACKET_POKER_TABLE], 1)
        d.addCallback(resendPackets)
        return d

    #-----------------------------------------------------------------------------
    translateConfigs = [ ('limit-2-4',  
"""<?xml version="1.0" encoding="UTF-8"?>
<bet xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="bet.xsd" name="2-4 limit" table-stakes="yes" unit="100" buy-in="1000" best-buy-in="6000" max-buy-in="100000000" poker_engine_version="1.3.0">
    <description>Limit 2/4</description>
    <blind small="100" big="200"/>
    <all_in method="side"/> 
    <variants ids="omaha omaha8 holdem">
      <round name="pre-flop" fixed="200" cap="3"/>
      <round name="flop" fixed="200" cap="3"/>
      <round name="turn" fixed="400" cap="3"/>
      <round name="river" fixed="400" cap="3"/>
    </variants>
</bet>
""", 'Limit 2/4', [] ),
    ( 'undescribed' , 
"""<?xml version="1.0" encoding="UTF-8"?>
<bet xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="bet.xsd" name="2-4 limit" table-stakes="yes" unit="100" buy-in="1000" best-buy-in="6000" max-buy-in="100000000" poker_engine_version="1.3.0">
    <blind small="100" big="200"/>
    <all_in method="side"/> 
    <variants ids="omaha omaha8 holdem">
      <round name="pre-flop" fixed="200" cap="3"/>
      <round name="flop" fixed="200" cap="3"/>
      <round name="turn" fixed="400" cap="3"/>
      <round name="river" fixed="400" cap="3"/>
    </variants>
</bet>
""" , 'undescribed',
["can't find readable name for 'undescribed'"]) ]
    def test23_checkTranslateToFileName(self):
        self.tmpdir = tempfile.mkdtemp()
        self.client_factory[0].dirs = [ self.tmpdir ]

        for (name, data, expectedReturn, expectedOutput) in self.translateConfigs:
            outFile = os.path.join(self.tmpdir, "poker.%s.xml" % name)
            outFH = open(outFile, "w")
            outFH.write(data)
            outFH.close()
            log_history.reset()
            self.assertEquals(self.client_factory[0].translateFile2Name(name), expectedReturn)
            self.assertEquals(log_history.get_all(), expectedOutput)

        shutil.rmtree(self.tmpdir)
        log_history.reset()
        return True
    # ---------------------------------------------------------------------------
    def updatePot(self, (client, packet)):
        class Game:
            def __init__(self):
                self.id = 1015
        g = Game()
        log_history.reset()
        packetCount = 0
        for p in client.updatePotsChips(g, None):
            self.assertEqual(p.game_id, 1015)
            self.assertEqual(p.type, PACKET_POKER_CHIPS_POT_RESET)
            packetCount += 1
        self.assertEquals(packetCount, 1)
        self.assertEqual(log_history.get_all(), [])
        return (client, packet)

    def test25_updatePotChipsNoSidePots(self):
        """Tests operation of PokerClientProtocol.updatePotChips"""
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.updatePot)
        return d
    # ---------------------------------------------------------------------------
    def updatePotWithSides(self, (client, packet), sides):
        class Game:
            def __init__(self):
                self.id = 1020
            unit = 1
        g = Game()
        packetCount = 0
        log_history.reset()
        packetList = client.updatePotsChips(g, sides)
        self.assertEqual(log_history.get_all(), ['normalizeChips: [1, 10] [1]', 'normalizeChips: [1, 40] [1]'])
        self.assertEquals(len(packetList), 2)
        for ii in [ 0, 1 ]:
            self.assertEquals(packetList[ii].game_id, 1020)
            self.assertEquals(packetList[ii].index, ii)
            self.assertEqual(packetList[ii].type, PACKET_POKER_POT_CHIPS)
        self.assertEqual(packetList[0].bet, [1, 10])
        self.assertEqual(packetList[1].bet, [1, 40])
        return (client, packet)

    def test26_updatePotChipsNoSidePots(self):
        """Tests operation of PokerClientProtocol.updatePotChips"""
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.updatePotWithSides, { 'pots' : [(10, 20), (40, 50) ]})
        return d
    # ---------------------------------------------------------------------------
    def changingURLHandlePacketInfo(self, (client, packet), forceCrash=False):
        skin = client.factory.getSkin()

        global specialCallCountforURL
        specialCallCountforURL = 0
        def misInterpretURLforSkin(url, outfit, force=forceCrash):
            global specialCallCountforURL
            specialCallCountforURL += 1
            if force and specialCallCountforURL > 1:
                return ("http://thatisthree", outfit)
            return ("http://thatistwo", outfit)
        correctInterpret = skin.interpret
        skin.interpret = misInterpretURLforSkin

        log_history.reset()
        client.handlePlayerInfo(PacketPokerPlayerInfo(
            name = "test",
            url = "http://thatisone/",
            outfit = "Stablize",
            serial = client.getSerial()
        ))
        if forceCrash:
            self.assertEquals(log_history.get_all(), ['PACKET_POKER_PLAYER_INFO: may enter loop packet.url = http://thatisone/\n url = http://thatistwo\n url_check = http://thatisthree\npacket.outfit = Stablize\n outfit = Stablize\n outfit_check = Stablize'])
        else:
            self.assertEquals(log_history.get_all(), ['sendPacket: POKER_PLAYER_INFO  type = 87 length = 46 serial = 4 game_id = 0 name = test outfit = Stablize url = http://thatistwo'])

        skin.interpret = correctInterpret
        return (client, packet)

    def test27_playerInfoPacket(self):
        """Tests when the url returned by the skin.interpret changes
        twice inbetween calls."""
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.changingURLHandlePacketInfo)
        return d

    def test28_playerInfoPacket(self):
        """Tests when the url returned by the skin.interpret changes
        three times."""
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.changingURLHandlePacketInfo, True)
        return d
    # ---------------------------------------------------------------------------
    def changingOutfitHandlePacketInfo(self, (client, packet), forceCrash=False):
        skin = client.factory.getSkin()

        global specialCallCountforOutfit
        specialCallCountforOutfit = 0
        def misInterpretOutfitforSkin(url, outfit, force=forceCrash):
            global specialCallCountforOutfit
            specialCallCountforOutfit += 1
            if force and specialCallCountforOutfit > 1:
                return (url, "OutfitThree")
            return (url, "OutfitTwo")
        correctInterpret = skin.interpret
        skin.interpret = misInterpretOutfitforSkin

        log_history.reset()
        client.handlePlayerInfo(PacketPokerPlayerInfo(
            name = "test",
            url = "http://stable/",
            outfit = "OutfitOne",
            serial = client.getSerial()
        ))
        if forceCrash:
            self.assertEquals(log_history.get_all(), ['PACKET_POKER_PLAYER_INFO: may enter loop packet.url = http://stable/\n url = http://stable/\n url_check = http://stable/\npacket.outfit = OutfitOne\n outfit = OutfitTwo\n outfit_check = OutfitThree'])
        else:
            self.assertEquals(log_history.get_all(), ['sendPacket: POKER_PLAYER_INFO  type = 87 length = 44 serial = 4 game_id = 0 name = test outfit = OutfitTwo url = http://stable/'])

        skin.interpret = correctInterpret
        return (client, packet)

    def test29_playerInfoPacketChangingOutfit(self):
        """Tests when the outfit returned by the skin.interpret changes
        twice inbetween calls."""
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.changingOutfitHandlePacketInfo)
        return d

    def test30_playerInfoPacketChangingOutfit(self):
        """Tests when the outfit returned by the skin.interpret changes
        three times."""
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.changingOutfitHandlePacketInfo, True)
        return d
    # ---------------------------------------------------------------------------
    def badPlayerObjectSetPlayerDelay(self, (client, packet)):
        class Game:
            def getPlayer(self, serial):
                return None
        g = Game()
        log_history.reset()
        client.setPlayerDelay(g, client.getSerial(), 101873)
        self.assertEquals(log_history.get_all(), [ "setPlayerDelay for a non-existing player %d" % client.getSerial() ])
        log_history.reset()
        return (client, packet)

    def test31_badPlayerObjectSetPlayerDelay(self):
        """Tests the action of setPlayerDelay when the player object
        returned by game is None"""
        d = self.client_factory[0].established_deferred
        d.addCallback(self.login, 0)
        d.addCallback(self.badPlayerObjectSetPlayerDelay)
        return d

    # ---------------------------------------------------------------------------
    def setCrashing(self, (client,)):
        self.client_factory[0].crashing = True
        return (client,)
        
    def test32_crashClient(self):
        d = self.client_factory[0].established_deferred
        d.addCallback(self.ping)
        d.addCallback(self.setCrashing)
        return d

    # ---------------------------------------------------------------------------
    def publishDeadPacket(self, client):
        if client.publish_packets == []:
            client.publish_packets.append(PacketPing())
        log_history.reset()
        client.publishPacket()
        self.assertEquals(log_history.get_all(), ['publishPacket: skip because connection not established'])
        return (client,)
        
    def setupDeadPacketPublish(self, client):
        d = client.connection_lost_deferred
        d.addCallback(self.publishDeadPacket)
        return (client,)

    def test33_sendPacketAfterLost(self):
        d = self.client_factory[0].established_deferred
        d.addCallback(self.setupDeadPacketPublish)
        return d

# -------------------------------------------------------------------------------
class PokerClientFactoryMockup(pokerclient.PokerClientFactory):
    def __init__(self, *args, **kwargs):
        pokerclient.PokerClientFactory.__init__(self, *args, **kwargs)
        self.call_networkNotAvailable = False
        self.call_networkAvailable = False
        self.rsync_host_no_network = False
        self.rsync_host_not_responding = False
        self.resolve_return_error = False

    def resolve(self, url):
        if self.resolve_return_error is True:
            return defer.fail(url)
        else:
            return defer.succeed(url)

    def networkNotAvailable(self):
        self.call_networkNotAvailable = True

    def networkAvailable(self):
        self.call_networkAvailable = True

    def failedUpgradeHostDoesNotRespond(self, logs, reason):
        self.rsync_host_not_responding = True

    def failedUpgradeNoNetwork(self, logs, reason):
        self.rsync_host_no_network = True

class PokerClientFactoryTestCase(unittest.TestCase):

    timeout = 500
    
    def setUp(self):
        log_history.reset()
        testclock._seconds_reset()        
        settings = pokernetworkconfig.Config([])
        settings.loadFromString(settings_xml_client)
        self.client_factory = PokerClientFactoryMockup(settings = settings)

    def tearDown(self):
        pass

    def checkResultFalse(self, d):
        self.assertEqual(True, self.client_factory.call_networkNotAvailable)
        self.assertEqual(False, self.client_factory.call_networkAvailable)

    def checkResultTrue(self, d):
        self.assertEqual(False, self.client_factory.call_networkNotAvailable)
        self.assertEqual(True, self.client_factory.call_networkAvailable)

    def testCheckNetworkSucess(self):
        self.resolve_return_error = False
        return self.client_factory.checkNetwork("dummy").addCallback(self.checkResultTrue)

    def testCheckNetworkError(self):
        self.resolve_return_error = True
        return self.client_factory.checkNetwork("dummy").addErrback(self.checkResultFalse)


class SettingMockup:
    def __init__(self):
        self.return_batch = False
        self.return_upgrade = False
        
    def headerGet(self, path):
        if path == "/settings/@batch":
            return self.return_batch
        if path == "/settings/@upgrades":
            return self.return_upgrade
        return None

# ------------------------------------------------------
from pokernetwork.pokerclient import PokerSkin

class PokerSkinMethodUnitTest(unittest.TestCase):

    timeout = 500
    
    # ---------------------------------------------------------------------------
    def setUp(self):
        pass
    # ---------------------------------------------------------------------------
    def tearDown(self):
        pass
    # ---------------------------------------------------------------------------
    def test00_destroyDoesNothing(self):
        skin = PokerSkin(settings = 'testing')
        skin.destroy()
        self.assertEquals(skin.url, "")
        self.assertEquals(skin.outfit, "")
        self.assertEquals(skin.settings, "testing")
    # ---------------------------------------------------------------------------
    def test02_getAndSetURL(self):
        skin = PokerSkin(settings = 'testing')
        self.assertEquals(skin.getUrl(), "")
        skin.setUrl("http://www.example.org/poker")
        self.assertEquals(skin.getUrl(), "http://www.example.org/poker")
    # ---------------------------------------------------------------------------
    def test03_getAndSetOutfit(self):
        skin = PokerSkin(settings = 'testing')
        self.assertEquals(skin.getOutfit(), "")
        skin.setOutfit("naked")
        self.assertEquals(skin.getOutfit(), "naked")
    # ---------------------------------------------------------------------------
    def test04_hideOutfitEditorDoesNothing(self):
        skin = PokerSkin(settings = 'testing')
        skin.hideOutfitEditor()
        self.assertEquals(skin.url, "")
        self.assertEquals(skin.outfit, "")
        self.assertEquals(skin.settings, "testing")
    # ---------------------------------------------------------------------------
    def test05_showOutfitEditorDoesNothing(self):
        skin = PokerSkin(settings = 'testing')
        skin.showOutfitEditor(None)
        self.assertEquals(skin.url, "")
        self.assertEquals(skin.outfit, "")
        self.assertEquals(skin.settings, "testing")
# ------------------------------------------------------
settings_xml_client_noChatNoDelays = """<?xml version="1.0" encoding="UTF-8"?>
<settings display2d="yes" display3d="no" ping="15000" verbose="6" delays="true" tcptimeout="2000" upgrades="no">
  <screen fullscreen="no" width="1024" height="768"/>
  <name>user1</name>
  <passwd>password1</passwd>
  <remember>yes</remember>
  <muck>yes</muck>
  <auto_post>no</auto_post>
  <web browser="/usr/bin/firefox">http://localhost/poker-web/</web>
  <sound>yes</sound>
  <tournaments currency_serial="1" type="sit_n_go" sort="name"/>
  <lobby currency_serial="1" type="holdem" sort="name"/>
  <shadow>yes</shadow>
  <vprogram>yes</vprogram>

  <path>%(engine_path)s/conf %(tests_path)s/conf</path>
  <data path="data" sounds="data/sounds"/>
  <handlist start="0" count="10"/>
</settings>
""" % {
    'dbhost': config.test.mysql.host,
    'dbname': config.test.mysql.database,
    'dbuser': config.test.mysql.user.name,
    'dbuser_password': config.test.mysql.user.password,
    'dbroot': config.test.mysql.root_user.name,
    'dbroot_password': config.test.mysql.root_user.password,
    'tests_path': TESTS_PATH,
    'engine_path': config.test.engine_path,
    'mysql_command': config.test.mysql.command
}

settings_xml_client_delaysWithRoundNoBlindAnte = """<?xml version="1.0" encoding="UTF-8"?>
<settings display2d="yes" display3d="no" ping="15000" verbose="6" delays="true" tcptimeout="2000" upgrades="no">
   <delays position="66" round="42" begin_round="77" end_round="99" end_round_last="0" showdown="0" lag="60"/> 
  <screen fullscreen="no" width="1024" height="768"/>
  <name>user1</name>
  <passwd>password1</passwd>
  <remember>yes</remember>
  <muck>yes</muck>
  <auto_post>no</auto_post>
  <web browser="/usr/bin/firefox">http://localhost/poker-web/</web>
  <sound>yes</sound>
  <tournaments currency_serial="1" type="sit_n_go" sort="name"/>
  <lobby currency_serial="1" type="holdem" sort="name"/>
  <shadow>yes</shadow>
  <vprogram>yes</vprogram>

  <path>%(engine_path)s/conf %(tests_path)s/conf</path>
  <data path="data" sounds="data/sounds"/>
  <handlist start="0" count="10"/>
</settings>
""" % {
    'dbhost': config.test.mysql.host,
    'dbname': config.test.mysql.database,
    'dbuser': config.test.mysql.user.name,
    'dbuser_password': config.test.mysql.user.password,
    'dbroot': config.test.mysql.root_user.name,
    'dbroot_password': config.test.mysql.root_user.password,
    'tests_path': TESTS_PATH,
    'engine_path': config.test.engine_path,
    'mysql_command': config.test.mysql.command
}

class PokerClientFactoryUnitMethodCoverageTestCase(unittest.TestCase):
    """These tests cover the methods in PokerClientFactory that are not
    otherwise covered by other tests in this file.  They are not
    particularly "aware" tests, they simply exercise various parts of the
    code to be sure the expected behavior occurs in the context of the
    individual methods."""

    timeout = 500
    
    # ---------------------------------------------------------------------------
    def setUp(self):
        self.settings = pokernetworkconfig.Config([])
        self.settings.loadFromString(settings_xml_client)
    # ---------------------------------------------------------------------------
    def tearDown(self): pass
    # ---------------------------------------------------------------------------
    def test00_initWithBadConfig(self):
        config_xml = """<?xml version="1.0" encoding="UTF-8"?>
<nothing/>"""
        config = pokernetworkconfig.Config([])
        config.loadFromString(config_xml)
        caughtIt = False
        try:
            clientFactory = pokerclient.PokerClientFactory(
                settings = self.settings, config = config)
            self.fail("previous line should have thrown exception")
        except UserWarning, uw:
            self.assertEquals(uw.__str__(), "PokerClientFactory: no /sequence/chips found in <string>")
            caughtIt = True
        self.failUnless(caughtIt, "Should have caught an Exception")
    # ---------------------------------------------------------------------------
    def test01_initWithEmptySequence(self):
        config_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sequence>
</sequence>"""
        config = pokernetworkconfig.Config([])
        config.loadFromString(config_xml)
        caughtIt = False
        try:
            clientFactory = pokerclient.PokerClientFactory(
                settings = self.settings, config = config)
            self.fail("previous line should have thrown exception")
        except UserWarning, uw:
            self.assertEquals(uw.__str__(), "PokerClientFactory: no /sequence/chips found in <string>")
            caughtIt = True
        self.failUnless(caughtIt, "Should have caught an Exception")
    # ---------------------------------------------------------------------------
    def test02_initWithstringValues(self):
        config_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sequence>
<chips>10 bar</chips>
</sequence>"""
        config = pokernetworkconfig.Config([])
        config.loadFromString(config_xml)
        caughtIt = False
        try:
            clientFactory = pokerclient.PokerClientFactory(
                settings = self.settings, config = config)
            self.fail("previous line should have thrown exception")
        except ValueError, ve:
            self.assertEquals(ve.__str__(),
                              "invalid literal for int() with base 10: 'bar'")
            caughtIt = True
        self.failUnless(caughtIt, "Should have caught an Exception")
    # ---------------------------------------------------------------------------
    def test03_init_configWithProperInts(self):
        config_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sequence>
<chips>5 200 10 15 20</chips>
</sequence>"""
        config = pokernetworkconfig.Config([])
        config.loadFromString(config_xml)

        clientFactory = pokerclient.PokerClientFactory(settings = self.settings,
                                                 config = config)
        self.assertEquals(clientFactory.chips_values,
                          [5, 200, 10, 15, 20])
    # ---------------------------------------------------------------------------
    def test04_init_missingChatMissingDelays(self):
        settings = pokernetworkconfig.Config([])
        settings.loadFromString(settings_xml_client_noChatNoDelays)
        clientFactory = pokerclient.PokerClientFactory(settings = settings)
        self.assertEquals(clientFactory.delays, {})
        self.assertEquals(clientFactory.chat_config, {})
    # ---------------------------------------------------------------------------
    def test05_init_roundPositionOverrides(self):
        settings = pokernetworkconfig.Config([])
        settings.loadFromString(settings_xml_client_delaysWithRoundNoBlindAnte)

        # Confirm the delays are properly read from the file before init'ing
        # PokerClientFactory
        delays = settings.headerGetProperties("/settings/delays")[0]
        for (key, value) in delays.iteritems():
            delays[key] = float(value)
        self.assertEquals(delays["end_round"], 99)
        self.assertEquals(delays["begin_round"], 77)
        self.assertEquals(delays["round"], 42)
        self.assertEquals(delays["position"], 66)
        self.failIf(delays.has_key("blind_ante_position"))

        clientFactory = pokerclient.PokerClientFactory(settings = settings)

        self.assertEquals(len(clientFactory.delays.keys()), 7)
        self.assertEquals(clientFactory.delays['lag'], 60.0)
        self.assertEquals(clientFactory.delays['end_round_last'], 0.0)
        self.assertEquals(clientFactory.delays['showdown'], 0.0)
        self.assertEquals(clientFactory.delays['blind_ante_position'], 66.0)
        self.assertEquals(clientFactory.delays['begin_round'], 42.0)
        self.assertEquals(clientFactory.delays['position'], 66.0)
        self.assertEquals(clientFactory.delays['end_round'], 42.0)
    # ---------------------------------------------------------------------------
    def test06_delRemovesGames(self):
        clientFactory = pokerclient.PokerClientFactory(settings = self.settings)
        global mockGamesDelCount
        mockGamesDelCount = 0
        class MockGames():
            def __del__(self):
                global mockGamesDelCount
                mockGamesDelCount += 1
        clientFactory.games = MockGames()
        clientFactory.__del__()
        self.assertEquals(mockGamesDelCount, 1)
    # ---------------------------------------------------------------------------
    def test07_resolve(self):
        clientFactory = pokerclient.PokerClientFactory(settings = self.settings)

        realResolve = reactor.resolve

        global myResolveCallCount
        myResolveCallCount = 0
        def myResolve(url, stuff):
            global myResolveCallCount
            myResolveCallCount += 1
            self.assertEquals(url, "http://example.org")
            self.assertEquals(stuff, (1,1))

        reactor.resolve = myResolve
        clientFactory.resolve("http://example.org")
        reactor.resolve = realResolve
        self.assertEquals(myResolveCallCount, 1)
    # ---------------------------------------------------------------------------
    def test08_checkNetwork_ForceHostNotResolved(self):
        clientFactory = pokerclient.PokerClientFactory(settings = self.settings)

        realResolve = reactor.resolve
        saveNetworkNotAvailable = clientFactory.networkNotAvailable

        resolveDeferred = defer.Deferred()
        def myResolve(url, stuff):
            return resolveDeferred

        mustGetCalledBackForTestSuccessDefferred = defer.Deferred()
        def myNetworkNotAvailable():
            reactor.resolve = realResolve  # Restore reactor's resolve
            clientFactory.networkNotAvailable = saveNetworkNotAvailable
            mustGetCalledBackForTestSuccessDefferred.callback(True)

        reactor.resolve = myResolve
        clientFactory.networkNotAvailable = myNetworkNotAvailable

        resolveDeferred.errback(True)
        return defer.DeferredList([
            clientFactory.checkNetwork("http://example.org"),
            mustGetCalledBackForTestSuccessDefferred])
    # ---------------------------------------------------------------------------
    def restoreReactorOs(self):
        sys.argv = self.saveSysArgv
        sys.executable = self.saveExecutable
        reactor.disconnectAll = self.saveReactorDisconnectAll
# ------------------------------------------------------

def GetTestSuite():
    loader = runner.TestLoader()
    # loader.methodPrefix = "_test"
    suite = loader.suiteFactory()
    suite.addTest(loader.loadClass(PokerClientTestCase))
    suite.addTest(loader.loadClass(PokerClientFactoryTestCase))
    suite.addTest(loader.loadClass(PokerSkinMethodUnitTest))
    suite.addTest(loader.loadClass(PokerClientFactoryUnitMethodCoverageTestCase))
    return suite

def Run():
    return runner.TrialRunner(
        reporter.TextReporter,
        tracebackFormat='default',
    ).run(GetTestSuite())

# ------------------------------------------------------
if __name__ == '__main__':
    if Run().wasSuccessful():
        sys.exit(0)
    else:
        sys.exit(1)
