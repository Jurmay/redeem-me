from flask import Flask, render_template, request, make_response
from flask_socketio import SocketIO, emit, join_room
import os
import random
from hero_ai_logic import hero_ai_decision
import io
import csv
from itertools import combinations

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret!"

# Use threading to avoid eventlet issues on Render
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

TABLE_ROOM = "table_main"
HERO_SEAT = 0

RANKS_ORDER = "23456789TJQKA"
RANK_VALUE = {r: i for i, r in enumerate(RANKS_ORDER, start=2)}


# ---------- Models ----------

class PlayerStats:
    """Simple per-seat stats for profiling."""
    def __init__(self):
        self.hands_played = 0
        self.vpip_hands = 0
        self.pfr_hands = 0

    def vpip_pct(self):
        return 0.0 if self.hands_played == 0 else 100.0 * self.vpip_hands / self.hands_played

    def pfr_pct(self):
        return 0.0 if self.hands_played == 0 else 100.0 * self.pfr_hands / self.hands_played


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

        # history and stats
        self.hand_history = []  # list of finished hands
        self.stats = {}         # seat -> PlayerStats

        # hero mode: "auto" or "manual"
        self.hero_mode = "auto"


game = GameState()
sid_to_seat = {}


# ---------- Helpers ----------

def make_deck():
    ranks = "23456789TJQKA"
    suits = "shdc"  # spades, hearts, diamonds, clubs
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


def record_hand(result):
    """Keep a copy of each finished hand for CSV export."""
    hand_copy = {
        "board": list(result.get("board", [])),
        "note": result.get("note", ""),
        "players": [
            {
                "seat": p["seat"],
                "name": p["name"],
                "hole_cards": list(p["hole_cards"]),
                "folded": p["folded"],
                "winner": p.get("winner", False),
                # keep best_five if present
                "best_five": list(p.get("best_five", [])),
            }
            for p in result.get("players", [])
        ],
    }
    game.hand_history.append(hand_copy)


def broadcast_stats():
    stats_view = []
    for p in game.players:
        st = game.stats.get(p.seat)
        if not st:
            continue
        stats_view.append({
            "seat": p.seat,
            "name": p.name,
            "hands_played": st.hands_played,
            "vpip_pct": round(st.vpip_pct(), 1),
            "pfr_pct": round(st.pfr_pct(), 1),
        })
    socketio.emit("stats_update", {"stats": stats_view}, room=TABLE_ROOM)


def reset_game():
    global game, sid_to_seat
    game = GameState()
    sid_to_seat = {}
    socketio.emit("table_reset", room=TABLE_ROOM)
    broadcast_state()
    broadcast_stats()

def soft_reset():
    """Clear current hand state but keep players and stats."""
    # Clear per-player hand state
    for p in game.players:
        p.hole_cards = []
        p.folded = False

    # Clear table / hand state
    game.deck = []
    game.full_board = []
    game.board = []
    game.pot = 0
    game.street = None
    game.current_bet = 0
    game.current_player_seat = None
    game.to_act = set()
    game.hand_running = False

    # Tell clients that hole cards are now empty
    for p in game.players:
        if p.sid:
            socketio.emit("hole_cards", {"cards": []}, room=p.sid)

    hero = find_player_by_seat(HERO_SEAT)
    if hero:
        socketio.emit("hero_hole_cards", {"cards": []}, room=TABLE_ROOM)

    broadcast_state()
    broadcast_stats()

def evaluate_5(cards5):
    """Return (category, tiebreakers list) for 5-card hand.
    category: 8=straight flush, 7=four of a kind, 6=full house,
              5=flush, 4=straight, 3=three of a kind, 2=two pair,
              1=one pair, 0=high card.
    """
    ranks = [RANK_VALUE[c[0]] for c in cards5]
    suits = [c[1] for c in cards5]

    ranks_sorted = sorted(ranks, reverse=True)
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1

    by_count = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    is_flush = len(set(suits)) == 1

    # Straight detection (including wheel A-5)
    uniq = sorted(set(ranks))
    is_straight = False
    high_straight = 0
    if len(uniq) >= 5:
        for i in range(len(uniq) - 4):
            window = uniq[i:i+5]
            if window[4] - window[0] == 4:
                is_straight = True
                high_straight = window[4]
        if {14, 2, 3, 4, 5}.issubset(set(ranks)):
            is_straight = True
            high_straight = 5

    if is_flush and is_straight:
        return (8, [high_straight])

    # Four of a kind
    if by_count[0][1] == 4:
        four = by_count[0][0]
        kicker = max(r for r in ranks if r != four)
        return (7, [four, kicker])

    # Full house
    if by_count[0][1] == 3 and len(by_count) > 1 and by_count[1][1] >= 2:
        three = by_count[0][0]
        pair = by_count[1][0]
        return (6, [three, pair])

    # Flush
    if is_flush:
        return (5, ranks_sorted)

    # Straight
    if is_straight:
        return (4, [high_straight])

    # Three of a kind
    if by_count[0][1] == 3:
        three = by_count[0][0]
        kickers = sorted([r for r in ranks if r != three], reverse=True)[:2]
        return (3, [three] + kickers)

    # Two pair
    if by_count[0][1] == 2 and len(by_count) > 1 and by_count[1][1] == 2:
        high_pair = max(by_count[0][0], by_count[1][0])
        low_pair = min(by_count[0][0], by_count[1][0])
        kicker = max(r for r in ranks if r not in (high_pair, low_pair))
        return (2, [high_pair, low_pair, kicker])

    # One pair
    if by_count[0][1] == 2:
        pair = by_count[0][0]
        kickers = sorted([r for r in ranks if r != pair], reverse=True)[:3]
        return (1, [pair] + kickers)

    # High card
    return (0, ranks_sorted)


def evaluate_7(cards7):
    """Best 5-card hand out of 7, returning rank only."""
    best = None
    for combo in combinations(cards7, 5):
        rank = evaluate_5(combo)
        if best is None or rank > best:
            best = rank
    return best


def best_5_from_7(cards7):
    """Return (rank, best_five_cards) for best 5 out of 7."""
    best_rank = None
    best_combo = None
    for combo in combinations(cards7, 5):
        rank = evaluate_5(combo)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_combo = list(combo)
    return best_rank, best_combo


def build_showdown_result():
    """Create result dict including winner info and best 5-card hand."""
    players_info = []

    if len(game.board) < 5:
        for p in game.players:
            players_info.append({
                "seat": p.seat,
                "name": p.name,
                "hole_cards": list(p.hole_cards),
                "folded": p.folded,
                "winner": False,
                "best_five": [],
            })
        note = "Showdown with incomplete board – winner not evaluated."
        return {
            "board": list(game.board),
            "players": players_info,
            "note": note,
        }

    # First build base info
    for p in game.players:
        players_info.append({
            "seat": p.seat,
            "name": p.name,
            "hole_cards": list(p.hole_cards),
            "folded": p.folded,
            "winner": False,
            "best_five": [],
        })

    # Evaluate active players and track best 5-card combo
    active_evals = []
    seat_to_best5 = {}
    best_rank = None

    for p in game.players:
        if p.folded or len(p.hole_cards) < 2:
            continue
        cards7 = p.hole_cards + game.board
        rank, best5 = best_5_from_7(cards7)
        active_evals.append((p.seat, rank))
        seat_to_best5[p.seat] = best5
        if best_rank is None or rank > best_rank:
            best_rank = rank

    winners = [seat for seat, r in active_evals if r == best_rank]

    for info in players_info:
        if info["seat"] in winners:
            info["winner"] = True
        if info["seat"] in seat_to_best5:
            info["best_five"] = seat_to_best5[info["seat"]]

    if winners:
        winner_names = [
            f"Seat {info['seat']} – {info['name']}"
            for info in players_info if info["seat"] in winners
        ]
        note = "Winners: " + ", ".join(winner_names)
    else:
        note = "No active players to evaluate."

    return {
        "board": list(game.board),
        "players": players_info,
        "note": note,
    }


def seat_order_after(seat):
    seats = sorted(active_seats())
    if not seats:
        return None
    if seat is None:
        return seats[0]
    bigger = [s for s in seats if s > seat]
    return bigger[0] if bigger else seats[0]


def manual_set_board(board_str):
    """Hero can manually set flop/turn/river from UI."""
    if not game.hand_running:
        return
    cards = board_str.split()
    n = len(cards)
    if n < 3 or n > 5:
        return  # need at least flop

    game.board = cards[:]
    if n == 3:
        game.street = "flop"
    elif n == 4:
        game.street = "turn"
    else:
        game.street = "river"

    # Keep full_board consistent
    if not game.full_board:
        game.full_board = cards[:]
    else:
        full = list(game.full_board)
        for i in range(min(n, len(full))):
            full[i] = cards[i]
        if len(full) < 5:
            full.extend(cards[len(full):5])
        game.full_board = full[:5]

    broadcast_state()


def check_hand_end_if_one_left():
    """If only one active seat remains, immediately end the hand."""
    remaining = [s for s in active_seats()]
    if len(remaining) <= 1 and game.hand_running:
        players_info = []
        for p in game.players:
            players_info.append({
                "seat": p.seat,
                "name": p.name,
                "hole_cards": list(p.hole_cards),
                "folded": p.folded,
                "winner": (not p.folded),
                "best_five": [],
            })
        winner_names = [
            f"Seat {p['seat']} – {p['name']}"
            for p in players_info if p["winner"]
        ]
        note = "Hand ended because all but one player folded."
        if winner_names:
            note += " Winner: " + ", ".join(winner_names)

        result = {
            "board": list(game.board),
            "players": players_info,
            "note": note,
        }
        record_hand(result)
        socketio.emit("hand_result", result, room=TABLE_ROOM)
        game.hand_running = False
        broadcast_state()
        broadcast_stats()
        return True
    return False


# ---------- Dealing & Streets ----------

def deal_new_hand(hero_cards=None, board_cards=None):
    # Reset simple stacks and status each hand (not real bankroll tracking).
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

    hero_cards = hero_cards.split() if hero_cards else []
    board_cards = board_cards.split() if board_cards else []

    used = set(hero_cards + board_cards)
    game.deck = [c for c in game.deck if c not in used]

    # Precompute full board
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

    # Track hands played
    for p in game.players:
        if p.seat not in game.stats:
            game.stats[p.seat] = PlayerStats()
        game.stats[p.seat].hands_played += 1

    game.board = []
    seats = active_seats()
    game.to_act = set(seats)
    game.current_player_seat = seats[0] if seats else None

    # Send hole cards privately to humans
    for p in game.players:
        if not p.is_hero and p.sid:
            socketio.emit("hole_cards", {"cards": p.hole_cards}, room=p.sid)

    # Hero cards broadcast (hero UI will show them)
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
        # Showdown – evaluate and mark winners.
        result = build_showdown_result()
        record_hand(result)
        socketio.emit("hand_result", result, room=TABLE_ROOM)
        game.hand_running = False
        broadcast_state()
        broadcast_stats()
        return

    game.current_bet = 0
    game.to_act = set(active_seats())
    seats = active_seats()
    game.current_player_seat = seats[0] if seats else None
    broadcast_state()
    ask_for_action()


# ---------- Hero AI & Action Logic ----------

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
        if high_pair or big_ace or suited:
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


def ask_for_action():
    if not game.hand_running:
        return

    if not game.to_act:
        next_street_or_showdown()
        return

    seat = game.current_player_seat
    if seat is None:
        seats = active_seats()
        game.current_player_seat = seats[0] if seats else None
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
        decision["mode"] = game.hero_mode
        socketio.emit("hero_decision", decision, room=TABLE_ROOM)

        if game.hero_mode == "auto":
            apply_action(player, decision["action"], decision.get("amount", 0),
                         is_hero=True)
        # in manual mode we just wait for hero_action from UI
    else:
        if player.sid:
            to_call = game.current_bet
            socketio.emit("request_action", {
                "seat": player.seat,
                "to_call": to_call
            }, room=player.sid)
        else:
            # no human attached, just skip their action (they effectively fold later)
            game.to_act.discard(seat)
            game.current_player_seat = seat_order_after(seat)
            ask_for_action()


def apply_action(player, action, amount, is_hero=False):
    seat = player.seat
    if seat not in game.to_act or not game.hand_running:
        return

    is_preflop = (game.street == "preflop")
    stats = game.stats.get(seat)

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

        if is_preflop and stats and call_amount > 0:
            stats.vpip_hands += 1

    elif action == "RAISE":
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

        if is_preflop and stats and total_bet > 0:
            stats.vpip_hands += 1
            stats.pfr_hands += 1

        game.to_act = set(s for s in active_seats() if s != seat)

    # If only one left, end hand immediately
    if check_hand_end_if_one_left():
        return

    game.current_player_seat = seat_order_after(seat)
    broadcast_state()
    ask_for_action()


# ---------- Routes ----------

@app.route("/")
def player_page():
    return render_template("player.html")


@app.route("/hero")
def hero_page():
    return render_template("hero.html")


def _history_csv_response():
    """Download all completed hands as CSV – one row per player per hand."""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "hand_id",
        "board",
        "note",
        "seat",
        "name",
        "hole_cards",
        "folded",
        "winner",
        "best_five",
    ])

    for hand_id, hand in enumerate(game.hand_history, start=1):
        board_str = " ".join(hand["board"])
        note = hand.get("note", "")
        for p in hand["players"]:
            hole_str = " ".join(p["hole_cards"])
            best_str = " ".join(p.get("best_five", []))
            writer.writerow([
                hand_id,
                board_str,
                note,
                p["seat"],
                p["name"],
                hole_str,
                p["folded"],
                p.get("winner", False),
                best_str,
            ])

    csv_data = output.getvalue()
    resp = make_response(csv_data)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = "attachment; filename=poker_hands.csv"
    return resp


@app.route("/history.csv")
def history_csv():
    return _history_csv_response()


@app.route("/download_csv")
def download_history():
    return _history_csv_response()


# ---------- Socket.IO Handlers ----------

@socketio.on("hero_join")
def on_hero_join(data):
    join_room(TABLE_ROOM)
    emit("hero_joined", {"ok": True, "mode": game.hero_mode})


@socketio.on("join_table")
def on_join(data):
    name = data.get("name") or "Guest"
    sid = request.sid

    # Prevent duplicate names (case-insensitive)
    lowered = name.lower()
    for p in game.players:
        if p.name.lower() == lowered:
            emit("join_result", {"success": False, "error": "Name already in use"})
            return

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

    if seat not in game.stats:
        game.stats[seat] = PlayerStats()

    join_room(TABLE_ROOM)
    emit("join_result", {"success": True, "seat": seat})
    broadcast_state()
    broadcast_stats()


@socketio.on("player_leave")
def on_player_leave():
    sid = request.sid
    seat = sid_to_seat.get(sid)
    if seat is None:
        return
    p = find_player_by_seat(seat)
    if p:
        if game.hand_running:
            p.folded = True
            game.to_act.discard(seat)
            if check_hand_end_if_one_left():
                pass
        if p in game.players:
            game.players.remove(p)
    if sid in sid_to_seat:
        del sid_to_seat[sid]
    broadcast_state()
    broadcast_stats()


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    seat = sid_to_seat.get(sid)
    if seat is not None:
        p = find_player_by_seat(seat)
        if p:
            if game.hand_running:
                p.folded = True
                game.to_act.discard(seat)
                check_hand_end_if_one_left()
            if p in game.players:
                game.players.remove(p)
        del sid_to_seat[sid]
    broadcast_state()
    broadcast_stats()


@socketio.on("hero_start_hand")
def on_hero_start(data):
    # Ensure hero exists (seat 0)
    hero = find_player_by_seat(HERO_SEAT)
    if not hero:
        hero = PlayerState(seat=HERO_SEAT, name="Hero AI", is_hero=True, sid=None)
        game.players.insert(0, hero)
        if HERO_SEAT not in game.stats:
            game.stats[HERO_SEAT] = PlayerStats()

    hero_cards = data.get("hero_cards")    # e.g. "As Kd"
    board_cards = data.get("board_cards")  # optional "Ah Kc 7d 2s 3c"
    deal_new_hand(hero_cards=hero_cards, board_cards=board_cards)


@socketio.on("hero_reset")
def on_hero_reset():
    reset_game()


@socketio.on("reset_table")
def on_reset_table():
    reset_game()

@socketio.on("hero_soft_reset")
def on_hero_soft_reset():
    soft_reset()

@socketio.on("hero_set_mode")
def on_hero_set_mode(data):
    mode = (data.get("mode") or "auto").lower()
    if mode not in ("auto", "manual"):
        return
    game.hero_mode = mode
    socketio.emit("hero_mode", {"mode": mode}, room=TABLE_ROOM)

    # If it's hero's turn and we switch to auto, act immediately.
    hero = find_player_by_seat(HERO_SEAT)
    if mode == "auto" and hero and game.hand_running and HERO_SEAT in game.to_act:
        ask_for_action()


@socketio.on("hero_action")
def on_hero_action(data):
    hero = find_player_by_seat(HERO_SEAT)
    if not hero or not game.hand_running:
        return
    if HERO_SEAT not in game.to_act:
        return

    action = data.get("action")
    amount = int(data.get("amount") or 0)
    apply_action(hero, action, amount, is_hero=True)


@socketio.on("hero_set_board")
def on_hero_set_board(data):
    board_cards = data.get("board_cards") or ""
    manual_set_board(board_cards)


@socketio.on("hero_kick")
def on_hero_kick(data):
    seat = data.get("seat")
    if seat is None or seat == HERO_SEAT:
        return
    p = find_player_by_seat(seat)
    if not p:
        return

    # Find and notify the client, if any
    kick_sid = None
    for s, seat_num in list(sid_to_seat.items()):
        if seat_num == seat:
            kick_sid = s
            break

    if game.hand_running:
        p.folded = True
        game.to_act.discard(seat)
        check_hand_end_if_one_left()

    if p in game.players:
        game.players.remove(p)

    if kick_sid:
        socketio.emit("kicked", {"seat": seat}, room=kick_sid)
        del sid_to_seat[kick_sid]

    broadcast_state()
    broadcast_stats()


@socketio.on("player_action")
def on_player_action(data):
    seat = data.get("seat")
    action = data.get("action")
    amount = int(data.get("amount") or 0)
    player = find_player_by_seat(seat)
    if not player or not game.hand_running:
        return
    apply_action(player, action, amount, is_hero=False)


# ---------- Main ----------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True,
    )
