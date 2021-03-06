#
# -*- py-indent-offset: 4; coding: utf-8; mode: python -*-
#
# Copyright (C) 2006, 2007, 2008, 2009 Loic Dachary <loic@dachary.org>
# Copyright (C)             2008 Bradley M. Kuhn <bkuhn@ebb.org>
# Copyright (C)             2009 Johan Euphrosine <proppy@aminche.com>
# Copyright (C) 2004, 2005, 2006 Mekensleep <licensing@mekensleep.com>
#                                24 rue vieille du temple 75004 Paris
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
#  Bradley M. Kuhn <bkuhn@ebb.org> (2008-)
#  Henry Precheur <henry@precheur.org> (2004)
#

from twisted.internet import reactor
from twisted.python.runtime import seconds

from pokerengine.pokergame import PokerGameServer
from pokerengine import pokergame, pokertournament

from pokerpackets.packets import *
from pokerpackets.networkpackets import *
from pokernetwork.lockcheck import LockCheck

from pokernetwork import pokeravatar
from pokernetwork.pokerpacketizer import createCache, history2packets, private2public

from pokernetwork import log as network_log
log = network_log.get_child('pokertable')

class PokerAvatarCollection:

    log = log.get_child('PokerAvatarCollection')

    def __init__(self, prefix=''):
        self.serial2avatars = {}
        self.prefix = prefix

    def get(self, serial):
        """get a list of avatars with the given serial"""
        return self.serial2avatars.get(serial, [])

    def add(self, avatar):
        """add an avatar to the collection"""
        serial = avatar.getSerial()
        self.log.debug("add %d %s", serial, avatar)
        if serial not in self.serial2avatars:
            self.serial2avatars[serial] = []
        if avatar not in self.serial2avatars[serial]:
            self.serial2avatars[serial].append(avatar)

    def remove(self, avatar):
        """remove an avatar from the collection"""
        serial = avatar.getSerial()
        self.log.debug("remove %d %s", serial, avatar, refs=[('User', serial, int)])
        assert avatar in self.serial2avatars[serial], "expected %d avatar in %s" % (
            serial,
            str(self.serial2avatars[serial])
        )
        self.serial2avatars[serial].remove(avatar)
        if len(self.serial2avatars[serial]) <= 0:
            del self.serial2avatars[serial]

    def values(self):
        """
        returns a list of avatarlists.

        The avatarlists are grouped by their serials.
        """
        return self.serial2avatars.values()

    def itervalues(self):
        """
        returns an iterator of avatarlists

        The avatarlists are grouped by their serials.
        """
        return self.serial2avatars.itervalues()

    def isEmpty(self):
        """returns True if the the collection is empty"""
        return not bool(self.serial2avatars)


class PokerPredefinedDecks:

    def __init__(self, decks):
        self.decks = decks
        self.index = 0

    def shuffle(self, deck):
        deck[:] = self.decks[self.index][:]
        self.index += 1
        if self.index >= len(self.decks):
            self.index = 0


class PokerTable:

    TIMEOUT_DELAY_COMPENSATION = 2

    log = log.get_child('PokerTable')

    def __init__(self, factory, id=0, description=None):
        self.log = PokerTable.log.get_instance(self, refs=[
            ('Game', self, lambda table: table.game.id),
            ('Hand', self, lambda table: table.game.hand_serial if table.game.hand_serial > 1 else None)
        ])
        self.factory = factory
        settings = self.factory.settings
        self.game = PokerGameServer("poker.%s.xml", factory.dirs)
        self.game.prefix = "[Server]"
        self.history_index = 0
        predefined_decks = settings.headerGetList("/server/decks/deck")
        if predefined_decks:
            self.game.shuffler = PokerPredefinedDecks(map(
                lambda deck: self.game.eval.string2card(deck.split()),
                predefined_decks
            ))
        self.observers = []
        self.waiting = []
        self.rebuy_stack =[]
        self.game.id = id
        self.game.name = description["name"]
        self.game.setVariant(description["variant"])
        self.game.setBettingStructure(description["betting_structure"])
        self.game.setMaxPlayers(int(description["seats"]))
        self.game.forced_dealer_seat = int(description.get("forced_dealer_seat", -1))
        self.skin = description.get("skin") or "default"
        self.currency_serial = int(description.get("currency_serial", 0))
        self.playerTimeout = int(description.get("player_timeout", 60))
        self.muckTimeout = int(description.get("muck_timeout", 5))
        self.transient = 'transient' in description
        self.tourney = description.get("tourney", None)

        # max_missed_round can be configured on a per table basis, which
        # overrides the server-wide default
        self.max_missed_round = int(description.get("max_missed_round",factory.getMissedRoundMax()))

        self.delays = settings.headerGetProperties("/server/delays")[0]
        self.autodeal = settings.headerGet("/server/@autodeal") == "yes"
        self.autodeal_temporary = settings.headerGet("/server/users/@autodeal_temporary") == 'yes'
        self.cache = createCache()
        self.owner = 0
        self.avatar_collection = PokerAvatarCollection("Table%d" % id)
        self.timer_info = {
            "playerTimeout": None,
            "playerTimeoutSerial": 0,
            "playerTimeoutTime": None,
            "muckTimeout": None,
        }
        self.previous_dealer = -1
        self.game_delay = {
            "start": 0,
            "delay": 0,
        }
        self.update_recursion = False

        self.bet_limits = None
        self.rebuy_happend_allready = None
        # Lock Checker
        self._initLockCheck()

    def _warnLock(self):
        self._lock_check_locked = True
        game_id = self.game.id if hasattr(self, 'game') else '?'
        hand_serial = self.game.hand_serial if hasattr(self, 'game') else '?'
        self.log.warn("Table is locked! game_id: %s, hand_serial: %s", game_id, hand_serial)

    def isLocked(self):
        return self._lock_check_locked

    def isValid(self):
        """Returns true if the table has a factory."""
        return hasattr(self, "factory")

    def canBeDespawned(self):
        return not self.isRunning() and self.avatar_collection.isEmpty() and not self.observers and self.tourney is None

    def destroy(self):
        """Destroys the table and deletes it from factory.tables. Also informs connected avatars."""
        self.log.debug("destroy table %d", self.game.id)
        #
        # cancel DealTimeout timer
        self.cancelDealTimeout()
        #
        # cancel PlayerTimeout timers
        self.cancelPlayerTimers()
        #
        # destroy factory table
        self.factory.destroyTable(self.game.id)

        #
        # broadcast TableDestroy to connected avatars
        self.broadcast(PacketPokerTableDestroy(game_id=self.game.id))
        #
        # remove table from avatars
        for avatars in self.avatar_collection.itervalues():
            for avatar in avatars:
                del avatar.tables[self.game.id]
        #
        # remove table from oberservers
        for observer in self.observers:
            del observer.tables[self.game.id]
        #
        # cut connection from and to factory
        self.factory.deleteTable(self)
        del self.factory
        #
        # kill lock check timer
        self._stopLockCheck()

    def getName(self, serial):
        """Returns the name to the given serial"""
        avatars = self.avatar_collection.get(serial)
        return avatars[0].getName() if avatars else self.factory.getName(serial)

    def getPlayerInfo(self, serial):
        """Returns a PacketPlayerInfo to the given serial"""
        avatars = self.avatar_collection.get(serial)
        return avatars[0].getPlayerInfo() if avatars and avatars[0].user.isLogged() else self.factory.getPlayerInfo(serial)

    def listPlayers(self):
        """Returns a list of names of all Players in game"""
        return [
                (self.getName(serial), self.game.getPlayerMoney(serial), 0,)
                for serial in self.game.serialsAll()
        ]

    def cancelDealTimeout(self):
        """If there is a dealTimeout timer in timer_info cancel and delete it"""
        info = self.timer_info
        if 'dealTimeout' in info:
            if info["dealTimeout"].active():
                info["dealTimeout"].cancel()
            del info["dealTimeout"]

    def beginTurn(self):
        self._startLockCheck()
        self.cancelDealTimeout()
        if self.game.isEndOrNull():
            self.historyReset()
            tourney_serial = self.tourney.serial if self.tourney else None
            hand_serial = self.factory.createHand(self.game.id, tourney_serial)
            self.log.debug("Dealing hand %s/%d", self.game.name, hand_serial)
            self.game.setTime(seconds())
            self.game.beginTurn(hand_serial)
            for player in self.game.playersAll():
                player.getUserData()['ready'] = True

    def updatePlayersMoney(self, serials_chips, absolute_values=True):
        """\
            Warning! This function call will kill the current hand, if it is running
            right now. Every person will fold. If some players are allready
            all-in, or broke, they will get some money so they will not get kicked
            out of the game. Afterwards the players money is added.

            In case of an Error, False will be returned. It is possible that
            some players have money updated.

            If everything was ok, True will be returned.

            =================== ======================================================================
            serials_chips       is a list of tuples: [(serial, money), (serial, money), ...]
                                (you can reduce the the probability of errors when you use all
                                player serials that are sitting on the table)

                                It is important that absolute amounts must be positive or 0. And
                                relative amounts must not result in a negative value. Otherwise an
                                Error will be returned and the money of this player will not be
                                changed.
            absolute_values     will define if the chips value is an absolute or an relative value.
                                Absolute vaules are less likely to produce errors. The default value
                                is True, for an absolute interpretation.
            =================== ======================================================================
        """
        game = self.game
        serials = [s for s,c in serials_chips]
        if not game.isEndOrNull():
            # We need to force end this game:
            # check if the game would end if we just fold:
            broke_players = [p for p in game.playersAll() if p.money == 0]
            if broke_players:
                if len([p for p in broke_players if p.serial in serials]) != len(broke_players):
                    self.log.error("updatePlayersMoney: there are broke players, that have not a specified money amount")
                    return False

                # if absolute_values:
                for player in broke_players:
                    # We will save those players from getting kicked out of the game
                    # since we will give them an absolute amount of money in the end
                    player.money = 1

            loop_counter = 0
            while not game.isEndOrNull():
                loop_counter += 1
                if loop_counter > len(game.serial2player):
                    self.log.error("updatePlayersMoney: Infinity Loop, game could not be ended")
                    return False
                game.fold(game.getSerialInPosition())
            # update table so history wont mess with things after this
            self.update()

        cursor = self.factory.db.cursor()
        try:
            error = False
            ## game is stopped, now we can transfer the money
            for serial, chips in serials_chips:
                player = game.getPlayer(serial)
                if not player:
                    error = True
                    self.log.error("updatePlayersMoney: player %d does not exist", serial, refs=[('User', serial, int)])
                    continue
                if absolute_values or player in broke_players:
                    if chips < 0:
                        error = True
                        self.log.error("updatePlayersMoney: player %d cannot get a negative amount of chips (%d)", serial, chips, refs=[('User', serial, int)])
                        # eleminate the discrepance of player.money and the database value again
                        if player in broke_players: player.money = 0
                        continue
                    new_chips = chips
                else:
                    new_chips = player.money + chips
                    if new_chips < 0:
                        error = True
                        self.log.error(
                            "updatePlayersMoney: player %d cannot get a negative amount of new_chips (%d), old_chips (%d), relative (%d)",
                            serial, new_chips, player.money, chips, refs=[('User', serial, int)]
                        )
                        # eleminate the discrepance of player.money and the database value again
                        if player in broke_players: player.money = 0
                        continue
                player.money = new_chips
                # just note, after an table move, the information is vanished.
                # If we need to keep that info, we need change the palyer object and add this attribute
                # or we reuse the old player object after an rebuy.
                player.money_modified = True

                sql = "UPDATE user2table SET money = %s WHERE user_serial = %s AND table_serial = %s"
                params = (player.money, player.serial, game.id)
                cursor.execute(sql, params)
            return not error
        finally:
            cursor.close()




    def rebuyPlayersOnes(self):
        if self.rebuy_happend_allready == self.game.hand_serial: return

        if not self.game.isEndOrMuck():
            return False

        self.rebuy_happend_allready = self.game.hand_serial
        #
        # Rebuy all players now, if they issued a rebuy or are auto-rebuying.
        # We need to do it before we decide if we will autodeal the next round.
        # Otherwise it is possible, that we will not process it an the next round will
        # not be dealt.
        if not self.transient:
            self.rebuyAllPlayers()
        else:
            self.tourneyRebuyAllPlayers()

        return True

    def rebuyAllPlayers(self):
        self.log.debug("rebuy all players now")
        for serial, amount in self.rebuy_stack:
            self.rebuyPlayerRequestNow(serial, amount)

        self.rebuy_stack = []
        for player in self.game.playersAll():
            self.log.debug("player %r, auto_refill %r, auto_rebuy %r", player.serial, player.auto_refill, player.auto_rebuy, refs=[('User', player, lambda p: p.serial)])
            if self.game.isBroke(player.serial) and player.auto_rebuy != PacketSetOption.OFF:
                self.rebuyPlayerRequestNow(player.serial, self._getPrefferedRebuyAmount(player.auto_rebuy))
            if player.auto_refill != PacketSetOption.OFF:
                self.rebuyPlayerRequestNow(player.serial, self._getPrefferedRebuyAmount(player.auto_refill))

    def _getPrefferedRebuyAmount(self, value):
        if value == PacketSetOption.AUTO_REBUY_BEST:
            return self.game.bestBuyIn()
        elif value == PacketSetOption.AUTO_REBUY_MAX:
            return self.game.maxBuyIn()
        elif value == PacketSetOption.AUTO_REBUY_MIN:
            return self.game.buyIn()
        else:
            return 0

    def historyReset(self):
        self.history_index = 0
        self.cache = createCache()

    def toPacket(self):
        return PacketPokerTable(
            id=self.game.id,
            name = self.game.name,
            variant = self.game.variant,
            betting_structure = self.game.betting_structure,
            seats = self.game.max_players,
            players = self.game.allCount(),
            hands_per_hour = self.game.stats["hands_per_hour"],
            average_pot = self.game.stats["average_pot"],
            percent_flop = self.game.stats["percent_flop"],
            player_timeout = self.playerTimeout,
            muck_timeout = self.muckTimeout,
            observers = len(self.observers),
            waiting = len(self.waiting),
            skin = self.skin,
            currency_serial = self.currency_serial,
            tourney_serial = self.tourney and self.tourney.serial or 0
        )

    def broadcast(self, packets):
        """Broadcast a list of packets to all connected avatars on this table."""
        if type(packets) is not list:
            packets = [packets]
        for packet in packets:
            keys = self.game.serial2player.keys()
            self.log.debug("broadcast%s %s ", keys, packet)
            for serial in keys:
                # player may be in game but disconnected.
                for avatar in self.avatar_collection.get(serial):
                    avatar.sendPacket(private2public(packet, serial))
            for avatar in self.observers:
                avatar.sendPacket(private2public(packet, 0))

        self.factory.eventTable(self)

    def updateBetLimits(self, history):
        """Looks for changed bet limits and, if found, appends a new BetLimits packet to packets"""
        should_update = False
        for event in reversed(history):
            if event[0] in ("game", "round"):
                should_update = True
                break
        if should_update:
            bet_limits = self.game.betLimits() + (self.game.getChipUnit(), self.game.roundCap())
            if bet_limits != self.bet_limits:
                self.bet_limits = bet_limits
                return True
        return False

    def getBetLimits(self):
        limit_min, limit_max, limit_step, limit_cap = self.bet_limits
        limit_type = {"money": PacketPokerBetLimits.NO_LIMIT, "pot": PacketPokerBetLimits.POT_LIMIT}.get(limit_max, PacketPokerBetLimits.LIMIT)
        if limit_type != PacketPokerBetLimits.LIMIT:
            limit_max = 0
            limit_cap = 0

        return PacketPokerBetLimits(
            game_id = self.game.id,
            min = limit_min,
            max = limit_max,
            step = limit_step,
            cap = limit_cap,
            limit = limit_type
        )

    def syncDatabase(self, history):
        updates = {}
        serial2rake = {}
        for event in history:
            event_type = event[0]
            if event_type == "game":
                pass

            elif event_type == "wait_for":
                pass

            elif event_type == "rebuy":
                pass

            elif event_type == "buyOut":
                pass
                                
            elif event_type == "player_list":
                pass

            elif event_type == "round":
                pass

            elif event_type == "showdown":
                pass

            elif event_type == "rake":
                serial2rake = event[2]

            elif event_type == "muck":
                pass

            elif event_type == "position":
                pass

            elif event_type == "blind_request":
                pass

            elif event_type == "wait_blind":
                pass

            elif event_type == "blind":
                serial, amount, dead = event[1:]
                if serial not in updates:
                    updates[serial] = 0
                updates[serial] -= amount + dead

            elif event_type == "ante_request":
                pass

            elif event_type == "ante":
                serial, amount = event[1:]
                if serial not in updates:
                    updates[serial] = 0
                updates[serial] -= amount

            elif event_type == "all-in":
                pass

            elif event_type == "call":
                serial, amount = event[1:]
                if serial not in updates:
                    updates[serial] = 0
                updates[serial] -= amount

            elif event_type == "check":
                pass

            elif event_type == "fold":
                pass

            elif event_type == "raise":
                serial, amount = event[1:]
                if serial not in updates:
                    updates[serial] = 0
                updates[serial] -= amount

            elif event_type == "canceled":
                serial, amount = event[1:]
                if serial > 0 and amount > 0:
                    if serial not in updates:
                        updates[serial] = 0
                    updates[serial] += amount

            elif event_type == "end":
                showdown_stack = event[2]
                game_state = showdown_stack[0]
                for (serial, share) in game_state['serial2share'].iteritems():
                    if serial not in updates:
                        updates[serial] = 0
                    updates[serial] += share

            elif event_type == "sitOut":
                pass

            elif event_type == "sit":
                pass

            elif event_type == "leave":
                pass

            elif event_type == "finish":
                hand_serial = event[1]
                self.factory.saveHand(self.compressedHistory(self.game.historyGet()), hand_serial)
                self.factory.updateTableStats(self.game, len(self.observers), len(self.waiting))
                transient = 1 if self.transient else 0
                self.factory.databaseEvent(event = PacketPokerMonitorEvent.HAND, param1 = hand_serial, param2 = transient, param3 = self.game.id)
            else:
                self.log.warn("syncDatabase: unknown history type %s", event_type)

        for (serial, amount) in updates.iteritems():
            self.factory.updatePlayerMoney(serial, self.game.id, amount)

        for (serial, rake) in serial2rake.iteritems():
            self.factory.updatePlayerRake(self.currency_serial, serial, rake)

    def compressedHistory(self, history):
        new_history = []
        cached_pockets = None
        cached_board = None
        for event in history:
            event_type = event[0]
            if event_type in (
                'all-in', 'wait_for','blind_request',
                'muck','finish', 'leave','rebuy', 'buyOut'
            ):
                pass

            elif event_type == 'game':
                new_history.append(event)

            elif event_type == 'round':
                name, board, pockets = event[1:]
                if pockets != cached_pockets: cached_pockets = pockets
                else: pockets = None
                if board != cached_board: cached_board = board
                else: board = None
                new_history.append((event_type, name, board, pockets))

            elif event_type == 'showdown':
                board, pockets = event[1:]
                if pockets != cached_pockets: cached_pockets = pockets
                else: pockets = None
                if board != cached_board: cached_board = board
                else: board = None
                new_history.append((event_type, board, pockets))

            elif event_type in (
                'call', 'check', 'fold',
                'raise', 'canceled', 'position',
                'blind', 'ante', 'player_list',
                'rake', 'end', 'sit', 'sitOut'
            ):
                new_history.append(event)

            else:
                self.log.warn("compressedHistory: unknown history type %s ", event_type)

        return new_history

    def delayedActions(self, history):
        for event in history:
            event_type = event[0]
            if event_type == "game":
                self.game_delay = {
                    "start": seconds(),
                    "delay": float(self.delays["autodeal"])
                }
            elif event_type in ('round', 'position', 'showdown', 'finish'):
                self.game_delay["delay"] += float(self.delays[event_type])
            elif event_type == "leave":
                quitters = event[1]
                for serial, _seat in quitters:
                    self.factory.leavePlayer(serial, self.game.id, self.currency_serial)
                    for avatar in self.avatar_collection.get(serial)[:]:
                        self.seated2observer(avatar)

    def _eventInHistory(self, history, event_type):
        # Go through the history backwards, as the finish event is found at the end
        for event in reversed(history):
            if event[0] == event_type:
                return True
        return False
    
    def kickPlayerSittingOutTooLong(self, history):
        if self.tourney: return
        if self._eventInHistory(history, "finish"):
            for player in self.game.playersAll():
                if player.getMissedRoundCount() >= self.max_missed_round:
                    self.kickPlayer(player.serial)
                    
    def tourneyEndTurn(self, history):
        if not self.tourney: return
        if self._eventInHistory(history, "end"):
            return self.factory.tourneyEndTurn(self.tourney, self.game.id)

    def tourneyUpdateStats(self, history):
        if not self.tourney: return
        if self._eventInHistory(history, "finish"):
            self.factory.tourneyUpdateStats(self.tourney, self.game.id)
            
    def tourneyRebuyAllPlayers(self):
        if not self.tourney: return
        self.factory.tourneyRebuyAllPlayers(self.tourney, self.game.id)

    def autoDeal(self):
        self.cancelDealTimeout()
        self.rebuyPlayersOnes()
        if not self.allReadyToPlay():
            #
            # All avatars that fail to send a PokerReadyToPlay packet
            # within imposed delays after sending a PokerProcessingHand
            # are marked as bugous and their next PokerProcessingHand
            # request will be ignored.
            #
            for player in self.game.playersAll():
                if player.getUserData()['ready'] == False:
                    for avatar in self.avatar_collection.get(player.serial):
                        self.log.inform("Player %d missed timeframe for PokerReadyToPlay", player.serial, refs=[('User', player, lambda p: p.serial)])
        
        if self.shouldAutoDeal():
            self.beginTurn()
            self.update()

    def autoDealCheck(self, autodeal_check, delta):
        self.cancelDealTimeout()
        if autodeal_check > delta:
            self.log.debug("Autodeal for %d scheduled in %f seconds", self.game.id, delta)
            self.timer_info["dealTimeout"] = reactor.callLater(delta, self.autoDeal)
            return
        #
        # Issue a poker message to all players that are ready
        # to play.
        #
        serials = []
        for player in self.game.playersAll():
            if player.getUserData()['ready'] == True:
                serials.append(player.serial)
        if serials:
            self.broadcastMessage(PacketPokerMessage, "Waiting for players.\nNext hand will be dealt shortly.\n(maximum %d seconds)" % int(delta), serials)
        self.log.debug("AutodealCheck(2) for %d scheduled in %f seconds", self.game.id, delta)
        self.timer_info["dealTimeout"] = reactor.callLater(autodeal_check, self.autoDealCheck, autodeal_check, delta - autodeal_check)

    def broadcastMessage(self, message_type, message, serials=None):
        if serials == None:
            serials = self.game.serialsAll()
        connected_serials = [serial for serial in serials if self.avatar_collection.get(serial)]
        if not connected_serials:
            return False
        packet = message_type(game_id = self.game.id, string = message)
        for serial in connected_serials:
            for avatar in self.avatar_collection.get(serial):
                avatar.sendPacket(packet)
        return True

    def serialsWillingToPlay(self):
        # it is not enought to count people if auto_rebuy is on, but we mus
        # erase those who are sitout and still have money (a regular sitout)
        serials = \
            set(serial for (serial, _amount) in self.rebuy_stack) | \
            set(p.serial for p in self.game.playersAll() if ((p.auto_refill or p.auto_rebuy) and p.money <= 0)) | \
            set(self.game.serialsSit())
            
        return serials

    def tourneySerialsWillingToPlay(self):
        return self.factory.tourneySerialsRebuying(self.tourney, self.game.id) \
            if self.tourney \
            else set()
        
    def shouldAutoDeal(self):
        if self.factory.shutting_down:
            self.log.debug("Not autodealing because server is shutting down")
            return False
        if not self.autodeal:
            self.log.debug("No autodeal")
            return False
        if self.isRunning():
            self.log.debug("Not autodealing %d because game is running", self.game.id)
            return False
        if self.game.state == pokergame.GAME_STATE_MUCK:
            self.log.debug("Not autodealing %d because game is in muck state", self.game.id)
            return False
        if len(self.serialsWillingToPlay() | self.tourneySerialsWillingToPlay()) < 2:
            self.log.debug("Not autodealing %d because less than 2 players willing to play", self.game.id)
            return False
        if self.game.isTournament():
            if self.tourney:
                if self.tourney.state != pokertournament.TOURNAMENT_STATE_RUNNING:
                    self.log.debug("Not autodealing %d because in tournament state %s", self.game.id, self.tourney.state)
                    return False
        elif not self.autodeal_temporary:
            #
            # Do not auto deal a table where there are only temporary
            # users (i.e. bots)
            #
            only_temporary_users = True
            for serial in self.game.serialsSit():
                if not self.factory.isTemporaryUser(serial):
                    only_temporary_users = False
                    break
            if only_temporary_users:
                self.log.debug("Not autodealing because players are categorized as temporary")
                return False
        return True

    def scheduleAutoDeal(self):
        self.cancelDealTimeout()

        if not self.shouldAutoDeal():
            return False

        delay = self.game_delay["delay"]
        if not self.allReadyToPlay() and delay > 0:
            delta = (self.game_delay["start"] + delay) - seconds()
            autodeal_max = float(self.delays.get("autodeal_max", 120))
            delta = min(autodeal_max, max(0, delta))
            self.game_delay["delay"] = (seconds() - self.game_delay["start"]) + delta
        elif self.transient:
            delta = int(self.delays.get("autodeal_tournament_min", 15))
            if seconds() - self.game_delay["start"] > delta:
                delta = 0
        else:
            delta = 0
        self.log.debug("AutodealCheck scheduled in %f seconds", delta)
        autodeal_check = max(0.01, float(self.delays.get("autodeal_check", 15)))
        self.timer_info["dealTimeout"] = reactor.callLater(min(autodeal_check, delta), self.autoDealCheck, autodeal_check, delta)
        return True

    def updatePlayerUserData(self, serial, key, value):
        if self.game.isSeated(serial):
            player = self.game.getPlayer(serial)
            user_data = player.getUserData()
            if user_data[key] != value:
                user_data[key] = value
                self.update()

    def allReadyToPlay(self):
        status = True
        notready = []
        for player in self.game.playersAll():
            if player.getUserData()['ready'] == False:
                notready.append(str(player.serial))
                status = False
        if notready:
            self.log.debug("allReadyToPlay: waiting for %s", ",".join(notready))
        return status

    def readyToPlay(self, serial):
        # since we cannot change the readyToPlay packet to contain the hand serial
        # we have guess if a ready to play is sent out of order. When the game is not 
        # finished yet, could assume that this packet was send for the previous hand.
        if self.game.isEndOrMuck():
            self.updatePlayerUserData(serial, 'ready', True)
            return PacketAck()

    def processingHand(self, serial):
        self.updatePlayerUserData(serial, 'ready', False)
        return PacketAck()

    def update(self):
        if self.update_recursion:
            self.log.warn("unexpected recursion (ignored)", exc_info=1)
            return "recurse"
        self.update_recursion = True
        if not self.isValid():
            return "not valid"

        self.rebuyPlayersOnes()

        history = self.game.historyGet()
        history_len = len(history)
        history_tail = history[self.history_index:]

        try:
            self.updateTimers(history_tail)
            packets, self.previous_dealer, errors = history2packets(history_tail, self.game.id, self.previous_dealer, self.cache)
            for error in errors: self.log.warn("%s", error)
            self.syncDatabase(history_tail)
            self.delayedActions(history_tail)
            if self.updateBetLimits(history_tail):
                packets = [self.getBetLimits()] + packets
            if len(packets) > 0:
                self.broadcast(packets)

            if self.canBeDespawned():
                self.factory.despawnTable(self.game.id)
            
            if self.isValid():
                self.kickPlayerSittingOutTooLong(history_tail)
                self.tourneyEndTurn(history_tail)
                
            if self.isValid():                
                self.tourneyUpdateStats(history_tail)
                self.scheduleAutoDeal()
        finally:
            if history_len != len(history):
                self.log.error("%s length changed from %d to %d (i.e. %s was added)",
                    history,
                    history_len,
                    len(history),
                    history[history_len:]
                )
            if self.game.historyCanBeReduced():
                try:
                    self.game.historyReduce()
                except Exception:
                    self.log.error('history reduce error', exc_info=1)
            self.history_index = len(self.game.historyGet())
            self.update_recursion = False
        return "ok"

    def handReplay(self, avatar, hand):
        history = self.factory.loadHand(hand)
        if not history:
            return
        event_type, level, hand_serial, hands_count, time, variant, betting_structure, player_list, dealer, serial2chips = history[0]  # @UnusedVariable
        for player in self.game.playersAll():
            avatar.sendPacketVerbose(PacketPokerPlayerLeave(
                game_id = self.game.id,
                serial = player.serial,
                seat = player.seat
            ))
        self.game.reset()
        self.game.name = "*REPLAY*"
        self.game.setVariant(variant)
        self.game.setBettingStructure(betting_structure)
        self.game.setTime(time)
        self.game.setHandsCount(hands_count)
        self.game.setLevel(level)
        self.game.hand_serial = hand
        for serial in player_list:
            self.game.addPlayer(serial)
            self.game.getPlayer(serial).money = serial2chips[serial]
            self.game.sit(serial)
        if self.isJoined(avatar):
            avatar.join(self, reason=PacketPokerTable.REASON_HAND_REPLAY)
        else:
            self.joinPlayer(avatar, reason=PacketPokerTable.REASON_HAND_REPLAY)
        serial = avatar.getSerial()
        cache = createCache()
        packets, previous_dealer, errors = history2packets(history, self.game.id, -1, cache) #@UnusedVariable
        for packet in packets:
            if packet.type == PACKET_POKER_PLAYER_CARDS and packet.serial == serial:
                packet.cards = cache["pockets"][serial].toRawList()
            if packet.type == PACKET_POKER_PLAYER_LEAVE:
                continue
            avatar.sendPacketVerbose(packet)

    def isJoined(self, avatar):
        serial = avatar.getSerial()
        return avatar in self.observers or avatar in self.avatar_collection.get(serial)

    def isSeated(self, avatar):
        return self.isJoined(avatar) and self.game.isSeated(avatar.getSerial())

    def isSit(self, avatar):
        return self.isSeated(avatar) and self.game.isSit(avatar.getSerial())

    def isSerialObserver(self, serial):
        return serial in [avatar.getSerial() for avatar in self.observers]

    def isOpen(self):
        return self.game.is_open

    def isRunning(self):
        return self.game.isRunning()

    def isStationary(self):
        return self.game.isEndOrNull() and 'dealTimeout' not in self.timer_info

    def seated2observer(self, avatar):
        self.avatar_collection.remove(avatar)
        self.observers.append(avatar)

    def observer2seated(self, avatar):
        self.observers.remove(avatar)
        self.avatar_collection.add(avatar)

    def quitPlayer(self, avatar):
        serial = avatar.getSerial()
        if self.isSit(avatar):
            if self.isOpen():
                self.game.sitOutNextTurn(serial)
                self.game.autoPlayer(serial)
            else:
                self.game.autoPlayer(serial)
                self.broadcast(PacketPokerAutoFold(serial = serial, game_id = self.game.id))
        self.update()
        if self.isSeated(avatar):
            #
            # If not on a closed table, stand up
            if self.isOpen():
                if avatar.removePlayer(self, serial):
                    self.seated2observer(avatar)
                    self.factory.leavePlayer(serial, self.game.id, self.currency_serial)
                    self.factory.updateTableStats(self.game, len(self.observers), len(self.waiting))
                else:
                    self.update()
            else:
                # cannot quit a closed table, request ignored
                return False

        if self.isJoined(avatar):
            #
            # The player is no longer connected to the table
            self.destroyPlayer(avatar)

        return True

    def kickPlayer(self, serial):
        player = self.game.getPlayer(serial)
        seat = player and player.seat

        if not self.game.removePlayer(serial):
            self.log.warn("kickPlayer did not succeed in removing player %d from game %d",
                serial,
                self.game.id,
                refs=[('User', serial, int)]
            )
            return

        self.factory.leavePlayer(serial, self.game.id, self.currency_serial)
        self.factory.updateTableStats(self.game, len(self.observers), len(self.waiting))

        for avatar in self.avatar_collection.get(serial)[:]:
            self.seated2observer(avatar)

        self.broadcast(PacketPokerPlayerLeave(
            game_id = self.game.id,
            serial = serial,
            seat = seat
        ))

    def disconnectPlayer(self, avatar):
        serial = avatar.getSerial()
        if self.isSeated(avatar):
            self.game.getPlayer(serial).getUserData()['ready'] = True
            if self.isOpen():
                #
                # If not on a closed table, stand up.
                if avatar.removePlayer(self, serial):
                    self.seated2observer(avatar)
                    self.factory.leavePlayer(serial, self.game.id, self.currency_serial)
                    self.factory.updateTableStats(self.game, len(self.observers), len(self.waiting))
                else:
                    self.update()

        if self.isJoined(avatar):
            #
            # The player is no longer connected to the table
            self.destroyPlayer(avatar)

        return True

    def leavePlayer(self, avatar):
        serial = avatar.getSerial()
        if self.isSit(avatar):
            if self.isOpen():
                self.game.sitOutNextTurn(serial)
            self.game.autoPlayer(serial)
        self.update()
        if self.isSeated(avatar):
            #
            # If not on a closed table, stand up
            if self.isOpen():
                if avatar.removePlayer(self, serial):
                    self.seated2observer(avatar)
                    self.factory.leavePlayer(serial, self.game.id, self.currency_serial)
                    self.factory.updateTableStats(self.game, len(self.observers), len(self.waiting))
                elif avatar.buyOutPlayer(self, serial):
                    self.factory.buyOutPlayer(serial, self.game.id, self.currency_serial)
                else:
                    self.update()
            else:
                self.log.warn("cannot leave a closed table", refs=[('User', serial, int)])
                avatar.sendPacketVerbose(PacketPokerError(
                    game_id = self.game.id,
                    serial = serial,
                    other_type = PACKET_POKER_PLAYER_LEAVE,
                    code = PacketPokerPlayerLeave.TOURNEY,
                    message = "Cannot leave tournament table"
                ))
                return False

        return True

    def movePlayer(self, serial, to_game_id, reason=""):
        """
        moves a player to another table/game. Usually called from pokertournament

        arguments:
            reason:  will be passed to the avatar.join() function

        returns: nothing
        """
        avatars = self.avatar_collection.get(serial)[:]
        #
        # We are safe because called from within the server under
        # controlled circumstances.
        #
        old_player = self.game.getPlayer(serial).copy()
        self.movePlayerFrom(serial, to_game_id)
        for avatar in avatars:
            self.destroyPlayer(avatar)

        other_table = self.factory.getTable(to_game_id)
        for avatar in avatars:
            other_table.observers.append(avatar)
            other_table.observer2seated(avatar)

        money_check = self.factory.movePlayer(serial, self.game.id, to_game_id)
        if money_check != old_player.money:
            self.log.warn("movePlayer: player %d money %d in database, %d in memory", serial, money_check, old_player.money, refs=[('User', serial, int)])

        for avatar in avatars:
            avatar.join(other_table, reason=reason)
        other_table.movePlayerTo(old_player)
        other_table.sendNewPlayerInformation(serial)
        if not other_table.update_recursion:
            other_table.scheduleAutoDeal()
        self.log.debug("player %d moved from table %d to table %d", serial, self.game.id, to_game_id, refs=[('User', serial, int)])

    def sendNewPlayerInformation(self, serial):
        packets = self.newPlayerInformation(serial)
        self.broadcast(packets)

    def newPlayerInformation(self, serial):
        player_info = self.getPlayerInfo(serial)
        player = self.game.getPlayer(serial)
        nochips = 0
        packets = []
        packets.append(PacketPokerPlayerArrive(
            game_id = self.game.id,
            serial = serial,
            name = player_info.name,
            url = player_info.url,
            outfit = player_info.outfit,
            blind = player.blind,
            remove_next_turn = player.remove_next_turn,
            sit_out = player.sit_out,
            sit_out_next_turn = player.sit_out_next_turn,
            auto = player.auto,
            auto_blind_ante = player.auto_blind_ante,
            wait_for = player.wait_for,
            seat = player.seat,
            buy_in_payed = player.buy_in_payed
        ))

        if player.isAuto():
            packets.append(PacketPokerAutoFold(
                serial = player.serial,
                game_id = self.game.id,
            ))
        if self.factory.has_ladder:
            packet = self.factory.getLadder(self.game.id, self.currency_serial, player.serial)
            if packet.type == PACKET_POKER_PLAYER_STATS:
                packets.append(packet)
        packets.append(PacketPokerSeats(game_id = self.game.id, seats = self.game.seats()))
        packets.append(PacketPokerPlayerChips(
            game_id = self.game.id,
            serial = serial,
            bet = nochips,
            money = self.game.getPlayer(serial).money
        ))
        return packets

    def movePlayerTo(self, old_player):
        """
        adds a new player to this table.

        should be called on the table where the player is moved towards.

        returns: nothing
        """
        was_open = self.game.is_open
        if not was_open: self.game.open()
        serial = old_player.serial
        player = self.game.addPlayer(serial, name=old_player.name)
        player.setUserData(pokeravatar.DEFAULT_PLAYER_USER_DATA.copy())
        player.money = old_player.money
        player.buy_in_payed = True
        self.game.autoBlindAnte(serial)
        if not self.game.isBroke(serial) and not old_player.isSitOut():
            self.game.sit(serial)
        player.bot = old_player.isBot()
        player.auto = old_player.isAuto()
        player.auto_policy = old_player.auto_policy
        player.action_issued = old_player.action_issued
        if not was_open: self.game.close()

    def movePlayerFrom(self, serial, to_game_id):
        """
        will remove the Player from the current game.

        should be called on the table where the player is moved away.
        """
        game = self.game
        player = game.getPlayer(serial)
        self.broadcast(PacketPokerTableMove(
            game_id = game.id,
            serial = serial,
            to_game_id = to_game_id,
            seat = player.seat)
        )
        game.removePlayer(serial)

    def possibleObserverLoggedIn(self, avatar):
        if not self.game.getPlayer(avatar.getSerial()):
            return False
        self.observer2seated(avatar)
        self.game.comeBack(avatar.getSerial())
        return True

    def joinPlayer(self, avatar, reason=""):
        """
        will connect a player with a table

        in case he is allready connected, this function will do nothing but
        tell the avatar to send all packets that will be needed to resume a session.

        otherwise he will be added to the observers or the avatar_collection first.
        """
        serial = avatar.getSerial()
        #
        # Nothing to be done except sending all packets.
        # Useful in disconnected mode to resume a session.
        if self.isJoined(avatar):
            avatar.join(self, reason=reason)
            return True
        #
        # Next, test to see if we have reached the server-wide maximum for
        # seated/observing players.
        if not self.game.isSeated(avatar.getSerial()) and self.factory.joinedCountReachedMax():
            self.log.warn("joinPlayer: %d cannot join game %d because the server is full", serial, self.game.id, refs=[('User', serial, int)])
            avatar.sendPacketVerbose(PacketPokerError(
                game_id = self.game.id,
                serial = serial,
                other_type = PACKET_POKER_TABLE_JOIN,
                code = PacketPokerTableJoin.FULL,
                message = "This server has too many seated players and observers."
            ))
            return False
        #
        # Next, test to see if joining this table will cause the avatar to
        # exceed the maximum permitted by the server.
        if len(avatar.tables) >= self.factory.simultaneous:
            self.log.inform("joinPlayer: %d seated at %d tables (max %d)", serial, len(avatar.tables), self.factory.simultaneous, refs=[('User', serial, int)])
            return False

        #
        # Player is now an observer, unless he is seated
        # at the table.
        self.factory.joinedCountIncrease()
        if not self.game.isSeated(avatar.getSerial()):
            self.observers.append(avatar)
        else:
            self.avatar_collection.add(avatar)
        #
        # If it turns out that the player is seated
        # at the table already, presumably because he
        # was previously disconnected from a tournament
        # or an ongoing game.
        came_back = False
        if self.isSeated(avatar):
            #
            # Sit back immediately, as if we just seated
            came_back = self.game.comeBack(serial)
        avatar.join(self, reason=reason)

        if came_back:
            #
            # It does not hurt to re-sit the avatar but it
            # is needed for other clients to notice the arrival
            self._sitPlayer(serial)

        return True

    def seatPlayer(self, avatar, seat):
        """moves a player from the observers to a given seat on the table"""
        serial = avatar.getSerial()
        if not self.isJoined(avatar):
            self.log.error("player %d can't seat before joining", serial, refs=[('User', serial, int)])
            return False
        if self.isSeated(avatar):
            self.log.inform("player %d is already seated", serial, refs=[('User', serial, int)])
            return False
        if not self.game.canAddPlayer(serial):
            self.log.inform("table refuses to seat player %d", serial, refs=[('User', serial, int)])
            return False
        if seat != -1 and seat not in self.game.seats_left:
            self.log.inform("table refuses to seat player %d at seat %d", serial, seat, refs=[('User', serial, int)])
            return False

        amount = self.game.buyIn() if self.transient else 0
        minimum_amount = (self.currency_serial, self.game.buyIn())

        if not self.factory.seatPlayer(serial, self.game.id, amount, minimum_amount):
            return False

        self.observer2seated(avatar)

        avatar.addPlayer(self, seat)
        if amount > 0:
            avatar.setMoney(self, amount)

        self.factory.updateTableStats(self.game, len(self.observers), len(self.waiting))
        return True

    def sitOutPlayer(self, avatar):
        serial = avatar.getSerial()
        if not self.isSeated(avatar):
            self.log.warn("player %d can't sit out before getting a seat", serial, refs=[('User', serial, int)])
            return False
        #
        # silently do nothing if already sit out
        if not self.isSit(avatar):
            return True

        game = self.game
        if self.isOpen():
            if game.sitOutNextTurn(serial):
                self.broadcast(PacketPokerSitOut(
                    game_id = game.id,
                    serial = serial
                ))
        else:
            game.autoPlayer(serial)
            self.broadcast(PacketPokerAutoFold(
                game_id = game.id,
                serial = serial
            ))

        return True

    def chatPlayer(self, avatar, message):
        serial = avatar.getSerial()
        if not self.isJoined(avatar):
            self.log.error("player %d can't chat before joining", serial, refs=[('User', serial, int)])
            return False
        message = self.chatFilter(message)
        self.broadcast(PacketPokerChat(
            game_id = self.game.id,
            serial = serial,
            message = message+"\n"
        ))
        self.factory.chatMessageArchive(serial, self.game.id, message)

    def chatFilter(self, message):
        return self.factory.chat_filter.sub('poker', message) \
            if self.factory.chat_filter \
            else message

    def autoBlindAnte(self, avatar, auto):
        if not self.isSeated(avatar):
            self.log.warn("player %d can't set auto blind/ante before getting a seat", avatar.getSerial(), refs=[('User', avatar.getSerial(), int)])
            return False
        return avatar.autoBlindAnte(self, avatar.getSerial(), auto)

    def autoRefill(self, serial, auto):
        if serial not in self.game.serial2player:
            self.log.warn("player %d can't set auto refill before getting a seat", serial, refs=[('User', serial, int)])
            return False
        if auto not in (PacketSetOption.OFF, PacketSetOption.AUTO_REFILL_MIN, PacketSetOption.AUTO_REFILL_MAX, PacketSetOption.AUTO_REFILL_BEST):
            return False
        self.game.serial2player[serial].auto_refill = auto
        return True

    def autoRebuy(self, serial, auto):
        if serial not in self.game.serial2player:
            self.log.warn("player %d can't set auto rebuy before getting a seat", serial, refs=[('User', serial, int)])
            return False
        if auto not in (PacketSetOption.OFF, PacketSetOption.AUTO_REBUY_MIN, PacketSetOption.AUTO_REBUY_MAX, PacketSetOption.AUTO_REBUY_BEST):
            return False
        self.game.serial2player[serial].auto_rebuy = auto
        return True

    def muckAccept(self, avatar):
        if not self.isSeated(avatar):
            self.log.warn("player %d can't accept muck before getting a seat", avatar.getSerial(), refs=[('User', avatar.getSerial(), int)])
            return False
        return self.game.muck(avatar.getSerial(), want_to_muck=True)

    def muckDeny(self, avatar):
        if not self.isSeated(avatar):
            self.log.warn("player %d can't deny muck before getting a seat", avatar.getSerial(), refs=[('User', avatar.getSerial(), int)])
            return False
        return self.game.muck(avatar.getSerial(), want_to_muck=False)

    def sitPlayer(self, avatar):
        if not self.isSeated(avatar):
            self.log.warn("player %d can't sit before getting a seat", avatar.getSerial())
            return False
        return self._sitPlayer(avatar.getSerial())

    def _sitPlayer(self, serial):
        game = self.game
        #
        # It does not harm to sit if already sit and it
        # resets the autoPlayer/wait_for flag.
        #
        if game.sit(serial) or game.isSit(serial):
            self.broadcast(PacketPokerSit(
                game_id = game.id,
                serial = serial
            ))

    def destroyPlayer(self, avatar):
        self.factory.joinedCountDecrease()
        if avatar in self.observers:
            self.observers.remove(avatar)
        else:
            self.avatar_collection.remove(avatar)
        del avatar.tables[self.game.id]

        # despawn table if game is not running and nobody is connected
        if self.canBeDespawned():
            self.factory.despawnTable(self.game.id)

    def buyInPlayer(self, avatar, amount):
        if not self.isSeated(avatar):
            self.log.warn("player %d can't bring money to a table before getting a seat", avatar.getSerial(), refs=[('User', avatar, lambda a: a.getSerial())])
            return False

        if avatar.getSerial() in self.game.serialsPlaying():
            self.log.warn("player %d can't bring money while participating in a hand", avatar.getSerial(), refs=[('User', avatar, lambda a: a.getSerial())])
            return False

        if self.transient:
            self.log.warn("player %d can't bring money to a transient table", avatar.getSerial(), refs=[('User', avatar, lambda a: a.getSerial())])
            return False

        player = self.game.getPlayer(avatar.getSerial())
        if player and player.isBuyInPayed():
            self.log.warn("player %d already payed the buy-in", avatar.getSerial(), refs=[('User', avatar, lambda a: a.getSerial())])
            return False

        amount = self.factory.buyInPlayer(avatar.getSerial(), self.game.id, self.currency_serial, max(amount, self.game.buyIn()))
        avatar.sendPacketVerbose(PacketPokerBuyIn(
                game_id = self.game.id,
                serial = avatar.getSerial(),
                amount = amount,
            ))
        return avatar.setMoney(self, amount)

    def rebuyPlayerRequest(self, serial, amount):
        if self.game.isRebuyPossible():
            return self.rebuyPlayerRequestNow(serial, amount)
        else:
            self.rebuy_stack.append((serial, amount))

    def rebuyPlayerRequestNow(self, serial, amount):
        retval = self._rebuyPlayerRequestNow(serial, amount)
        if not retval:
            for avatar in self.avatar_collection.get(serial):
                avatar.sendPacketVerbose(PacketPokerError(
                    game_id = self.game.id,
                    serial = avatar.getSerial(),
                    other_type = PACKET_POKER_REBUY
                ))
        if retval is None:
            for avatar in self.avatar_collection.get(serial):
                self.leavePlayer(avatar)
            
        if retval:
            self.game.comeBack(serial)
            self.game.sit(serial)
        return retval

    def _rebuyPlayerRequestNow(self, serial, amount):
        if serial not in self.game.serial2player:
            self.log.warn("player %d can't rebuy to a table before getting a seat", serial, refs=[('User', serial, int)])
            return False

        player = self.game.getPlayer(serial)
        if not player.isBuyInPayed():
            self.log.warn("player %d can't rebuy before paying the buy in", serial, refs=[('User', serial, int)])
            return False

        # after a rebuy, the money the user has has to be between buyIn and maxBuyIn
        maximum = self.game.maxBuyIn() - self.game.getPlayerMoney(serial)
        minimum = self.game.buyIn() - self.game.getPlayerMoney(serial)
        amount = min(max(amount, minimum), maximum)
        
        if maximum <= 0:
            self.log.inform("player %d can't bring more money to the table", serial, refs=[('User', serial, int)])
            return False
        
        amount = self.factory.buyInPlayer(serial, self.game.id, self.currency_serial, amount)

        if amount == 0:
            self.log.inform("player %d is broke and cannot rebuy", serial, refs=[('User', serial, int)])
            return None

        if self.tourney:
            self.log.error("player %d cannot use PacketPokerRebuy to rebuy during tourney", serial, refs=[('User', serial, int)])
            return False

        if not self.game.rebuy(serial, amount):
            self.log.warn("player %d rebuy denied", serial, refs=[('User', serial, int)])
            return False

        return True

    def playerWarningTimer(self, serial):
        info = self.timer_info
        if self.game.isRunning() and serial == self.game.getSerialInPosition():
            timeout = self.playerTimeout / 2
            #
            # Compensate the communication lag by always giving the avatar
            # an extra 2 seconds to react. The warning says that there only is
            # N seconds left but the server will actually timeout after N + TIMEOUT_DELAY_COMPENSATION
            # seconds.
            self.broadcast(PacketPokerTimeoutWarning(
                game_id = self.game.id,
                serial = serial,
                timeout = timeout
            ))
            info["playerTimeout"] = reactor.callLater(timeout+self.TIMEOUT_DELAY_COMPENSATION, self.playerTimeoutTimer, serial)
        else:
            self.updatePlayerTimers()

    def playerTimeoutTimer(self, serial):
        self.log.debug("player %d times out", serial, refs=[('User', serial, int)])
        if self.game.isRunning() and serial == self.game.getSerialInPosition():
            if self.isOpen():
                self.game.sitOutNextTurn(serial)
                self.game.autoPlayer(serial)
            else:
                self.game.autoPlayer(serial)
                self.broadcast(PacketPokerAutoFold(serial = serial, game_id = self.game.id))
            self.broadcast(PacketPokerTimeoutNotice(serial = serial, game_id = self.game.id))
            self.update()
        else:
            self.updatePlayerTimers()

    def muckTimeoutTimer(self):
        self.log.debug("muck timed out")
        # timer expires, force muck on muckables not responding
        for serial in self.game.muckable_serials[:]:
            self.game.muck(serial, want_to_muck=True)
        self.cancelMuckTimer()
        self.update()

    def cancelMuckTimer(self):
        info = self.timer_info
        timer = info["muckTimeout"]
        if timer != None:
            if timer.active(): timer.cancel()
            info["muckTimeout"] = None

    def cancelPlayerTimers(self):
        info = self.timer_info
        timer = info["playerTimeout"]
        if timer != None:
            if timer.active(): timer.cancel()
            info["playerTimeout"] = None
        info["playerTimeoutSerial"] = 0
        info["playerTimeoutTime"] = None

    def updateTimers(self, history=()):
        self.updateMuckTimer(history)
        self.updatePlayerTimers()

    def updateMuckTimer(self, history):
        for event in reversed(history):
            if event[0] == "muck":
                self.cancelMuckTimer()
                self.timer_info["muckTimeout"] = reactor.callLater(self.muckTimeout, self.muckTimeoutTimer)
                return

    def updatePlayerTimers(self):
        info = self.timer_info
        if self.game.isRunning():
            serial = self.game.getSerialInPosition()
            #
            # any event in the game resets the player timeout
            if (
                info["playerTimeoutSerial"] != serial or
                len(self.game.historyGet()) > self.history_index
            ):
                timer = info["playerTimeout"]
                if timer != None and timer.active(): timer.cancel()
                timer = reactor.callLater(self.playerTimeout / 2, self.playerWarningTimer, serial)
                info["playerTimeout"] = timer
                info["playerTimeoutSerial"] = serial
                info["playerTimeoutTime"] = self.playerTimeout + seconds()
        else:
            #
            # if the game is not running, cancel the previous timeout
            self.cancelPlayerTimers()

    def getCurrentTimeoutWarning(self):
        info = self.timer_info
        packet = None
        if (
            self.game.isRunning() and
            info["playerTimeout"] is not None and
            info["playerTimeoutSerial"] != 0 and
            info["playerTimeoutTime"] is not None and
            info["playerTimeout"].active()
        ):
            serial = info["playerTimeoutSerial"]
            timeout = int(info["playerTimeoutTime"] - seconds())
            packet = PacketPokerTimeoutWarning(
                game_id = self.game.id,
                serial = serial,
                timeout = timeout
            )
        return packet


    def _initLockCheck(self):
        self._lock_check = LockCheck(20 * 60, self._warnLock)
        self.game.registerCallback(self.__lockCheckEndCallback)
        self._lock_check_locked = False

    def _startLockCheck(self):
        if self._lock_check and self.playerTimeout < self._lock_check._timeout:
            self._lock_check.start()

    def _stopLockCheck(self):
        if self._lock_check:
            self._lock_check.stop()

    def __lockCheckEndCallback(self, game_id, event_type, *args):
        if event_type == 'end_round_last':
            self._stopLockCheck()
