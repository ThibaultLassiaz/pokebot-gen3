from typing import Generator, Tuple

from modules.context import context
from modules.battle_state import BattleOutcome
from modules.map_data import MapFRLG, MapRSE, is_safari_map
from modules.region_map import FlyDestinationRSE
from modules.player import get_player, get_player_avatar, TileTransitionState
from modules.pokemon_party import get_party
from modules.memory import get_event_flag
from modules.menuing import StartMenuNavigator
from modules.modes.util.walking import wait_for_player_avatar_to_be_controllable
from modules.modes.util.higher_level_actions import unmount_bicycle
from modules.safari_strategy import (
    SafariPokemon,
    SafariHuntingMode,
    SafariHuntingObject,
    get_safari_pokemon,
    get_navigation_path,
    get_safari_balls_left,
    get_safari_zone_config,
)
from modules.runtime import get_sprites_path
from modules.gui.multi_select_window import Selection, ask_for_choice
from ._interface import BotMode, BotModeError
from ._asserts import (
    SavedMapLocation,
    assert_item_exists_in_bag,
    assert_save_game_exists,
    assert_saved_on_map,
    assert_boxes_or_party_can_fit_pokemon,
)
from .util import (
    spin,
    fish,
    fly_to,
    talk_to_npc,
    soft_reset,
    navigate_to,
    apply_repel,
    repel_is_active,
    ensure_facing_direction,
    wait_for_script_to_start_and_finish,
    wait_for_player_avatar_to_be_standing_still,
    wait_for_unique_rng_value,
    apply_white_flute_if_available,
    save_the_game,
)
from modules.modes.util.tasks_scripts import (
    wait_for_task_to_start_and_finish,
    wait_for_yes_no_question,
    wait_for_no_script_to_run,
)


class CakesSafariMode(BotMode):
    def __init__(self):
        self._safari_config = get_safari_zone_config(context.rom)
        self._starting_cash = None
        self._pokemon_caught = None
        self._should_reenter = False
        self._should_reset = False
        self._use_repel = False
        # You can adjust the number of runs below. Keep in mind you can only carry 30 Pokéblocks,
        # so spending around $15,000 is typically the upper limit per run.
        # After about 30 runs, you'll likely have used up all your Pokéblocks.
        # Once this spending limit is reached, the bot will exit the Safari Zone,
        # stop, and prompt you to save your game.
        # Here I've seen 300k for almost unlimited runs
        self._money_spent_limit = 300000

    @staticmethod
    def name() -> str:
        return "Cakes Safari"

    @staticmethod
    def is_selectable() -> bool:
        return get_player_avatar().map_group_and_number in (
            MapFRLG.FUCHSIA_CITY_SAFARI_ZONE_ENTRANCE,
            MapRSE.ROUTE121_SAFARI_ZONE_ENTRANCE,
        )

    def on_safari_zone_timeout(self):
        return True

    def on_battle_ended(self, outcome: "BattleOutcome") -> None:
        """
        Handle the outcome of a battle. If the battle resulted in a catch,
        update flags to manage the re-entry or reset logic.
        """
        if outcome is BattleOutcome.Caught:
            self._should_reenter = True
            self._atleast_one_pokemon_catched = True
            assert_boxes_or_party_can_fit_pokemon()
        if get_safari_balls_left() < 30:
            current_cash = get_player().money
            if (self._starting_cash - current_cash > self._money_spent_limit) or (current_cash < 500):
                self._should_reset = True
            else:
                self._should_reenter = True

    def run(self) -> Generator:
        stats = context.stats.get_global_stats().to_dict()
        self._starting_cash = get_player().money

        assert_save_game_exists("There is no saved game. Cannot start Safari mode. Please save your game.")

        assert_boxes_or_party_can_fit_pokemon()
        assert_boxes_or_party_can_fit_pokemon(check_in_saved_game=True)

        assert_saved_on_map(
            SavedMapLocation(self._safari_config["map"]),
            self._safari_config["save_message"],
        )

        pokemon_choice = self._get_next_target(stats)

        if pokemon_choice is None:
            context.message = "All required Pokémon caught. Safari is complete."
            context.set_manual_mode()
            return

        context.message = f"Current target: {pokemon_choice}"
        target = get_safari_pokemon(pokemon_choice)

        mode = ask_for_choice(
            [
                Selection("Use Repel", get_sprites_path() / "items" / "Repel.png"),
                Selection("No Repel", get_sprites_path() / "other" / "No Repel.png"),
            ],
            window_title="Use Repel?",
        )

        if mode is None:
            context.set_manual_mode()
            yield
            return

        if mode == "Use Repel":
            self._use_repel = True

        self._check_mode_requirement(target.value.mode, target.value.hunting_object)
        yield from self._check_map_requirement(target.value.map_location)

        while True:
            if self._should_reset:
                if not self._atleast_one_pokemon_catched:
                    yield from self._soft_reset()
                    self._starting_cash = get_player().money
                else:
                    if is_safari_map():
                        yield from self._exit_safari_zone()
                    context.message = f"You have hit the money threshold (either you've run out of funds or spent over {self._money_spent_limit}₽), but you managed to catch at least one Pokémon during this cycle. Consider saving your game."
                    context.set_manual_mode()
                    break
            elif self._should_reenter:
                yield from self._re_enter_safari_zone()

            stats = context.stats.get_global_stats().to_dict()
            pokemon_choice = self._get_next_target(stats)

            if pokemon_choice is None:
                context.message = "All required Pokémon caught. Safari is complete."
                context.set_manual_mode()
                break
                return

            context.message = f"Current target: {pokemon_choice}"

            target = get_safari_pokemon(pokemon_choice)
            self._check_mode_requirement(target.value.mode, target.value.hunting_object)
            yield from self._check_map_requirement(target.value.map_location)

            yield from self._start_safari_hunt(target)

    def _start_safari_hunt(self, safari_pokemon: SafariPokemon) -> Generator:
        current_cash = get_player().money
        if current_cash < 500:
            raise BotModeError("You do not have enough cash to enter the Safari Zone.")

        yield from navigate_to(self._safari_config["map"], self._safari_config["entrance_tile"])
        yield from ensure_facing_direction(self._safari_config["facing_direction"])

        context.emulator.hold_button(self._safari_config["facing_direction"])
        for _ in range(10):
            yield
        context.emulator.release_button(self._safari_config["facing_direction"])
        yield
        yield from wait_for_script_to_start_and_finish(self._safari_config["ask_script"], "A")
        yield from wait_for_script_to_start_and_finish(self._safari_config["enter_script"], "A")

        if context.rom.is_frlg:
            yield from wait_for_player_avatar_to_be_controllable()
        else:
            while (
                get_player_avatar().local_coordinates != (32, 35)
                or get_player_avatar().tile_transition_state != TileTransitionState.NOT_MOVING
            ):
                yield

        yield from self._navigate_and_hunt(
            safari_pokemon.value.map_location, safari_pokemon.value.tile_location, safari_pokemon.value.mode
        )

    def _re_enter_safari_zone(self) -> Generator:
        """Handles re-entry into the Safari Zone."""
        if is_safari_map():
            yield from self._exit_safari_zone()
        self._should_reenter = False
        return

    def _exit_safari_zone(self) -> Generator:
        """Handles re-entry into the Safari Zone."""
        yield from StartMenuNavigator("RETIRE").step()
        yield from wait_for_script_to_start_and_finish(self._safari_config["exit_script"], "A")
        yield from wait_for_player_avatar_to_be_standing_still()

    def _navigate_and_hunt(
        self, target_map: MapFRLG | MapRSE, tile_location: Tuple[int, int], mode: SafariHuntingMode
    ) -> Generator:

        def stop_condition():
            return self._should_reset or self._should_reenter

        def is_at_entrance_door():
            return self._safari_config["is_at_entrance_door"]()

        if is_at_entrance_door():
            yield from wait_for_player_avatar_to_be_standing_still()
        elif context.rom.is_rse and self._safari_config.get("is_script_active", lambda: False)():
            while is_at_entrance_door() or self._safari_config["is_script_active"]():
                yield
            yield from wait_for_player_avatar_to_be_standing_still()

        path = get_navigation_path(target_map, tile_location)

        for map_group, coords in path:
            yield from navigate_to(map_group, coords)

        if mode in (SafariHuntingMode.SPIN, SafariHuntingMode.SURF):
            if self._use_repel and not repel_is_active():
                yield from apply_repel()
            yield from unmount_bicycle()
            yield from apply_white_flute_if_available()
            yield from spin(stop_condition=stop_condition)
        elif mode == SafariHuntingMode.FISHING:
            yield from fish(stop_condition=stop_condition, loop=True)
        else:
            raise BotModeError(f"Error: Unknown mode {mode}.")

    def _check_mode_requirement(self, mode: SafariHuntingMode, object: SafariHuntingObject) -> bool:
        match mode:
            case SafariHuntingMode.SURF:
                if not (get_event_flag("BADGE05_GET") and get_party().has_pokemon_with_move("Surf")):
                    raise BotModeError(
                        f"Cannot start mode {mode.value}. You're missing Badge 05 or you don't have any Pokémon with Surf"
                    )
            case SafariHuntingMode.FISHING:
                assert_item_exists_in_bag(
                    object,
                    error_message=f"You need to own the {object} in order to hunt this Pokémon in the Safari Zone.",
                    check_in_saved_game=True,
                )
            case _:
                return True

    def _check_map_requirement(self, map: MapFRLG | MapRSE) -> Generator:
        match map:
            case MapRSE.SAFARI_ZONE_NORTHWEST:
                assert_item_exists_in_bag(
                    "Mach Bike",
                    error_message="You need to own the Mach Bike in order to hunt the next target in the Safari Zone.",
                    check_in_saved_game=True,
                )
            case MapRSE.SAFARI_ZONE_NORTH:
                try:
                    assert_item_exists_in_bag(
                        "Acro Bike",
                        error_message="You need to own the Acro Bike in order to hunt the next target in the Safari Zone.",
                        check_in_saved_game=True,
                    )
                except BotModeError:
                    yield from self._change_bike()
            case _:
                return True

    def _soft_reset(self) -> Generator:
        """Handles soft resetting if cash difference exceeds the limit."""
        yield from soft_reset()
        yield from wait_for_unique_rng_value()
        self._should_reset = False
        for _ in range(5):
            yield

    def _change_bike(self) -> Generator:
        yield from navigate_to(MapRSE.ROUTE121_SAFARI_ZONE_ENTRANCE, (14, 13))
        yield from fly_to(FlyDestinationRSE.MauvilleCity)
        yield from navigate_to(MapRSE.MAUVILLE_CITY, (35, 5))
        yield from talk_to_npc(1)
        yield from wait_for_yes_no_question("Yes")
        yield from wait_for_no_script_to_run("B")
        yield from wait_for_player_avatar_to_be_standing_still("B")
        yield from navigate_to(MapRSE.MAUVILLE_CITY_BIKE_SHOP, (3, 8))
        yield from fly_to(FlyDestinationRSE.LilycoveCity)
        yield from navigate_to(MapRSE.ROUTE121, (37, 5))
        yield from navigate_to(MapRSE.ROUTE121_SAFARI_ZONE_ENTRANCE, (9, 4))
        yield from save_the_game()

    def _get_next_target(self, stats: dict) -> str | None:
        pokemon_stats = stats.get("pokemon", {})

        def get_catches(name: str) -> int:
            return pokemon_stats.get(name, {}).get("catches", 0)

        zones = [
            (
                "southwest",
                {
                    "core": {"Psyduck": 2, "Pikachu": 2, "Girafarig": 1},
                    "combo": None,
                },
            ),
            (
                "northwest",
                {
                    "core": {"Rhyhorn": 2, "Pinsir": 1},
                    "combo": ("Doduo", "Dodrio"),
                },
            ),
            (
                "northeast",
                {
                    "core": {"Phanpy": 2, "Heracross": 1},
                    "combo": ("Natu", "Xatu"),
                },
            ),
        ]

        for zone_name, config in zones:
            core_missing = []

            for name, required in config["core"].items():
                if get_catches(name) < required:
                    core_missing.append(name)

            combo = config.get("combo")

            if core_missing:
                return core_missing[0]

            if combo:
                a, b = combo
                a_catches = get_catches(a)
                b_catches = get_catches(b)
                combo_ok = (a_catches >= 2) or (a_catches >= 1 and b_catches >= 1)
                if not combo_ok:
                    return a
        return None
