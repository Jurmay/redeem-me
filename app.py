from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import os
import random

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"

# IMPORTANT: force threading so eventlet is not used
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

TABLE_ROOM = "table_main"
HERO_SEAT = 0


class PlayerState:
    def __init__(self, seat, name, is_hero=False, sid=None):
        self.seat = seat
        self.name = name
        self.is_hero = is_hero
        self.sid = sid          # socket id for humans
        self.chips = 1000
        self.hole_cards = []    # list of "As", "Kd"
        self.folded = False


class GameState:
    def __init__(self):
        self.players = []       # list[PlayerState]
        self.deck = []
        self.full_board = []    # 5-card board precomputed
        self.board = []         # currently exposed board
        self.pot = 0
        self.street = None      # "preflop", "flop", "turn", "river"
        self.current_bet = 0
        self.current_player_seat = None
        self.to_act = set()     # seats that still need to act this street
        self.hand_running = False


game = GameState()
sid_to_seat = {}


def make_deck():
    ranks = "23456789TJQKA"
    suits = "shdc"
    deck = [r + s for r in ranks for s in suits]
    random.shuffle(deck)
    return deck


def find_player_by_seat(seat):
    for p in game.players:
        if p.seat == seat:
            return p
    return None


def active_seats():
    return [p.seat for p in game.players if not p.folded]


def broadcast_state():
    data = {
        "pot": game.pot,
        "board": game.board,
        "street": game.street,
        "players": [
            {
                "seat": p.seat,
                "name": p.name,
                "chips": p.chips,
                "folded": p.folded,
                "is_hero": p.is_hero,
            } for p in game.players
        ],
        "current_player_seat": game.current_player_seat,
    }
    socketio.emit("table_state", data, room=TABLE_ROOM)


def deal_new_hand(hero_cards=None, board_cards=None):
    # Reset simple stacks and status each hand (for learning, not bankroll tracking).
    for p in game.players:
        p.chips = 1000
        p.folded = False
        p.hole_cards = []

    game.deck = make_deck()
    game.pot = 0
    game.street = "preflop"
    game.board = []
    game.full_board = []
    game.current_bet = 0
    game.hand_running = True

    # Parse manual hero or board cards (e.g. "As Kd", "Ah Kc 7d 2s 3c")
    if hero_cards:
        hero_cards = hero_cards.split()
    else:
        hero_cards = []

    if board_cards:
        board_cards = board_cards.split()
    else:
        board_cards = []

    used = set(hero_cards + board_cards)
    game.deck = [c for c in game.deck if c not in used]

    # Precompute full board (5 cards)
    if board_cards and len(board_cards) == 5:
        game.full_board = board_cards[:]
    else:
        for _ in range(5):
            game.full_board.append(game.deck.pop())

    # Deal hole cards
    for p in game.players:
        if p.is_hero and hero_cards and len(hero_cards) == 2:
            p.hole_cards = hero_cards[:]
        else:
            p.hole_cards = [game.deck.pop(), game.deck.pop()]

    # Preflop: nothing on board yet
    game.board = []
    seats = active_seats()
    game.to_act = set(seats)
    game.current_player_seat = seats[0] if seats else None

    # Send hole cards privately to humans
    for p in game.players:
        if not p.is_hero and p.sid:
            socketio.emit("hole_cards", {"cards": p.hole_cards}, room=p.sid)

    # Hero cards broadcast (only hero.html will show them)
    hero = find_player_by_seat(HERO_SEAT)
    if hero:
        socketio.emit("hero_hole_cards", {"cards": hero.hole_cards}, room=TABLE_ROOM)

    broadcast_state()
    ask_for_action()


def next_street_or_showdown():
    if not game.hand_running:
        return

    if game.street == "preflop":
        game.street = "flop"
        game.board = game.full_board[:3]
    elif game.street == "flop":
        game.street = "turn"
        game.board = game.full_board[:4]
    elif game.street == "turn":
        game.street = "river"
        game.board = game.full_board[:5]
    else:
        # Showdown – for now, just reveal all hands and finish.
        result = {
            "board": game.board,
            "players": [
                {
                    "seat": p.seat,
                    "name": p.name,
                    "hole_cards": p.hole_cards,
                    "folded": p.folded,
                } for p in game.players
            ],
            "note": "Showdown – winner not auto-calculated in this demo."
        }
        socketio.emit("hand_result", result, room=TABLE_ROOM)
        game.hand_running = False
        broadcast_state()
        return

    # New street: reset simple betting trackers
    game.current_bet = 0
    game.to_act = set(active_seats())
    seats = active_seats()
    game.current_player_seat = seats[0] if seats else None
    broadcast_state()
    ask_for_action()


def seat_order_after(seat):
    seats = sorted(active_seats())
    if not seats:
        return None
    if seat is None:
        return seats[0]
    bigger = [s for s in seats if s > seat]
    if bigger:
        return bigger[0]
    return seats[0]


def ask_for_action():
    if not game.hand_running:
        return

    if not game.to_act:
        # Betting round over
        next_street_or_showdown()
        return

    seat = game.current_player_seat
    if seat is None:
        return

    if seat not in game.to_act:
        game.current_player_seat = seat_order_after(seat)
        ask_for_action()
        return

    player = find_player_by_seat(seat)
    if not player or player.folded:
        game.to_act.discard(seat)
        game.current_player_seat = seat_order_after(seat)
        ask_for_action()
        return

    if player.is_hero:
        decision = hero_ai_decision(player)
        socketio.emit("hero_decision", decision, room=TABLE_ROOM)
        apply_action(player, decision["action"], decision.get("amount", 0),
                     is_hero=True, reason=decision["reason"])
    else:
        # Ask human via socket
        if player.sid:
            to_call = game.current_bet
            socketio.emit("request_action", {
                "seat": player.seat,
                "to_call": to_call
            }, room=player.sid)
        else:
            game.to_act.discard(seat)
            game.current_player_seat = seat_order_after(seat)
            ask_for_action()


def hero_ai_decision(player):
    """Very simple heuristic AI with explanations."""
    rank_map = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
                "7": 7, "8": 8, "9": 9, "T": 10, "J": 11,
                "Q": 12, "K": 13, "A": 14}
    h1, h2 = player.hole_cards
    r1, r2 = rank_map[h1[0]], rank_map[h2[0]]
    suited = h1[1] == h2[1]
    high_pair = (h1[0] == h2[0]) and r1 >= 10
    big_ace = ("A" in (h1[0], h2[0])) and max(r1, r2) >= 11

    to_call = game.current_bet
    pot = game.pot

    if game.street == "preflop":
        if high_pair or big_ace:
            amount = max(20, pot + 10)
            reason = "Strong preflop hand; we raise to build the pot."
            return {"action": "RAISE", "amount": amount, "reason": reason}
        else:
            if to_call == 0:
                reason = "Marginal hand; we check to see a cheap flop."
                return {"action": "CHECK", "amount": 0, "reason": reason}
            else:
                reason = "Weak hand facing a bet; we fold."
                return {"action": "FOLD", "amount": 0, "reason": reason}
    else:
        if not game.board:
            reason = "No board yet; we check."
            return {"action": "CHECK", "amount": 0, "reason": reason}

        board_ranks = [c[0] for c in game.board]
        has_ace = "A" in (h1[0], h2[0])
        pair_with_board = (h1[0] in board_ranks) or (h2[0] in board_ranks) or (h1[0] == h2[0])

        if pair_with_board or has_ace:
            if to_call == 0:
                bet = max(20, pot // 2 or 20)
                reason = "Decent made hand; we bet for value."
                return {"action": "RAISE", "amount": bet, "reason": reason}
            else:
                reason = "Decent made hand; we call to continue."
                return {"action": "CALL", "amount": 0, "reason": reason}
        else:
            if to_call == 0:
                reason = "Nothing strong; we check."
                return {"action": "CHECK", "amount": 0, "reason": reason}
            else:
                reason = "Weak hand facing a bet; we fold."
                return {"action": "FOLD", "amount": 0, "reason": reason}


def apply_action(player, action, amount, is_hero=False, reason=None):
    seat = player.seat
    if seat not in game.to_act or not game.hand_running:
        return

    if action == "FOLD":
        player.folded = True
        game.to_act.discard(seat)

    elif action == "CHECK":
        game.to_act.discard(seat)

    elif action == "CALL":
        call_amount = min(player.chips, game.current_bet)
        player.chips -= call_amount
        game.pot += call_amount
        game.to_act.discard(seat)

    elif action == "RAISE":
        # Simple model: call up to current_bet, then raise "amount"
        call_amount = min(player.chips, game.current_bet)
        total_bet = call_amount + amount
        total_bet = min(total_bet, player.chips + call_amount)
        pay = total_bet
        if pay > 0:
            if pay > player.chips:
                pay = player.chips
            player.chips -= pay
            game.pot += pay
        game.current_bet = total_bet
        # Others must act again
        game.to_act = set(s for s in active_seats() if s != seat)

    # If only one left, end hand immediately
    remaining = [s for s in active_seats()]
    if len(remaining) <= 1 and game.hand_running:
        result = {
            "board": game.board,
            "players": [
                {
                    "seat": p.seat,
                    "name": p.name,
                    "hole_cards": p.hole_cards,
                    "folded": p.folded,
                } for p in game.players
            ],
            "note": "Hand ended because all but one player folded."
        }
        socketio.emit("hand_result", result, room=TABLE_ROOM)
        game.hand_running = False
        broadcast_state()
        return

    # Move to next player
    game.current_player_seat = seat_order_after(seat)
    broadcast_state()
    ask_for_action()


@app.route("/")
def player_page():
    return render_template("player.html")


@app.route("/hero")
def hero_page():
    return render_template("hero.html")


@socketio.on("hero_join")
def on_hero_join(data):
    # Hero/admin joins the common table room for updates
    join_room(TABLE_ROOM)
    emit("hero_joined", {"ok": True})


@socketio.on("join_table")
def on_join(data):
    name = data.get("name") or "Guest"
    sid = request.sid

    # Assign free seat 1..6
    used_seats = {p.seat for p in game.players}
    seat = None
    for s in range(1, 7):
        if s not in used_seats:
            seat = s
            break

    if seat is None:
        emit("join_result", {"success": False, "error": "Table full"})
        return

    p = PlayerState(seat=seat, name=name, is_hero=False, sid=sid)
    game.players.append(p)
    sid_to_seat[sid] = seat
    join_room(TABLE_ROOM)

    emit("join_result", {"success": True, "seat": seat})
    broadcast_state()


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    seat = sid_to_seat.get(sid)
    if seat is not None:
        p = find_player_by_seat(seat)
        if p:
            p.folded = True
        del sid_to_seat[sid]
    broadcast_state()


@socketio.on("hero_start_hand")
def on_hero_start(data):
    # Ensure hero exists (seat 0)
    hero = find_player_by_seat(HERO_SEAT)
    if not hero:
        hero = PlayerState(seat=HERO_SEAT, name="Hero (AI)", is_hero=True, sid=None)
        game.players.insert(0, hero)

    hero_cards = data.get("hero_cards")    # e.g. "As Kd"
    board_cards = data.get("board_cards")  # e.g. "Ah Kc 7d 2s 3c"
    deal_new_hand(hero_cards=hero_cards, board_cards=board_cards)


@socketio.on("player_action")
def on_player_action(data):
    seat = data.get("seat")
    action = data.get("action")
    amount = int(data.get("amount") or 0)
    player = find_player_by_seat(seat)
    if not player or not game.hand_running:
        return
    apply_action(player, action, amount, is_hero=False)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
