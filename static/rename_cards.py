import os

# FULL ABSOLUTE PATH
FOLDER = r"C:\static\cards_raw"
OUT_FOLDER = r"C:\static\cards_renamed"

os.makedirs(OUT_FOLDER, exist_ok=True)

rank_map = {
    "ace": "A",
    "king": "K",
    "queen": "Q",
    "jack": "J",
    "10": "T",
    "9": "9",
    "8": "8",
    "7": "7",
    "6": "6",
    "5": "5",
    "4": "4",
    "3": "3",
    "2": "2",
}

suit_map = {
    "spades": "s",
    "hearts": "h",
    "diamonds": "d",
    "clubs": "c",
}

for filename in os.listdir(FOLDER):
    if not filename.lower().endswith(".png"):
        continue

    base = filename.replace(".png", "")
    parts = base.split("_of_")  # ace_of_spades → ["ace", "spades"]

    if len(parts) != 2:
        print("Skipped:", filename)
        continue

    rank_word, suit_word = parts

    rank = rank_map.get(rank_word)
    suit = suit_map.get(suit_word)

    if not rank or not suit:
        print("Unknown:", filename)
        continue

    new_name = f"{rank}{suit}.png"

    old_path = os.path.join(FOLDER, filename)
    new_path = os.path.join(OUT_FOLDER, new_name)

    os.rename(old_path, new_path)
    print(f"{filename} → {new_name}")
