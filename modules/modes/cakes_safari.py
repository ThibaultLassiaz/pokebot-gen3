import random
from typing import Generator, Tuple
from datetime import datetime, timedelta

from modules.context import context
from modules.battle_state import BattleOutcome
from modules.map_data import MapFRLG, MapRSE, is_safari_map
from modules.map_path import calculate_path
from modules.items import get_item_bag, get_item_by_name
from modules.region_map import FlyDestinationRSE
from modules.player import get_player, get_player_avatar, TileTransitionState
from modules.pokemon_party import get_party
from modules.memory import get_event_flag, read_symbol, unpack_uint16
from modules.menuing import StartMenuNavigator
from modules.modes.util.walking import wait_for_player_avatar_to_be_controllable
from modules.modes.util.higher_level_actions import unmount_bicycle, put_pokeblock_in_feeder
from modules.pokeblock_feeder import get_active_pokeblock_feeder_for_location
from modules.safari_strategy import (
    SafariPokemon,
    SafariHuntingMode,
    SafariHuntingObject,
    RSESafariStrategy,
    get_safari_pokemon,
    get_navigation_path,
    get_safari_balls_left,
    get_safari_zone_config,
    get_lowest_feel_pokeblock_by_type,
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
from modules.tasks import is_waiting_for_input
from modules.modes.util.tasks_scripts import (
    wait_for_task_to_start_and_finish,
    wait_for_yes_no_question,
    wait_for_no_script_to_run,
)


class CakesSafariMode(BotMode):
    def __init__(self):
        self._safari_config = get_safari_zone_config(context.rom)
        self._starting_cash = None
        self._should_reenter = False
        self._should_reset = False
        self._should_save = False
        self._use_repel = False
        self._current_map = None
        self._current_tile = None
        self._feeder_direction = None
        self._pokeblock_type_in_feeder = "spicy"
        self._do_easter_egg_after_battle = False
        self._money_spent_limit = 300000
        self._easter_egg_timestamp = None
        self._atleast_one_pokemon_catched = False

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
        steps_remaining_symbol = "sSafariZoneStepCounter"
        steps_remaining = unpack_uint16(read_symbol(steps_remaining_symbol))

        location = (self._current_map, self._current_tile)
        rest_house = (MapRSE.SAFARI_ZONE_SOUTHWEST, (32, 8))

        to_rest = calculate_path(
            location,
            rest_house,
            avoid_encounters=True,
            avoid_scripted_events=True,
            has_acro_bike=self._has_bike("Acro Bike"),
            has_mach_bike=self._has_bike("Mach Bike"),
        )
        from_rest = calculate_path(
            rest_house,
            location,
            avoid_encounters=True,
            avoid_scripted_events=True,
            has_acro_bike=self._has_bike("Acro Bike"),
            has_mach_bike=self._has_bike("Mach Bike"),
        )

        if outcome == BattleOutcome.Lost:
            if len(to_rest) + len(from_rest) + 10 < steps_remaining:
                yield from self._easter_egg_rest(self._current_map, self._current_tile)
            if get_safari_balls_left() < 30:
                current_cash = get_player().money
                if (self._starting_cash - current_cash > self._money_spent_limit) or (current_cash < 500):
                    self._should_reset = True
                else:
                    self._should_reenter = True

        elif outcome == BattleOutcome.RanAway:
            if self._easter_egg_timestamp and datetime.now() >= self._easter_egg_timestamp:
                self._do_easter_egg_after_battle = True
                self._easter_egg_timestamp = None

        elif outcome == BattleOutcome.Caught:
            self._should_reenter = True
            self._should_save = True
            self._atleast_one_pokemon_catched = True
            assert_boxes_or_party_can_fit_pokemon()

        if get_safari_balls_left() < 30:
            current_cash = get_player().money
            if (self._starting_cash - current_cash > self._money_spent_limit) or (current_cash < 500):
                self._should_reset = True
            else:
                self._should_reenter = True

    def run(self) -> Generator:
        self._starting_cash = get_player().money

        assert_save_game_exists("There is no saved game. Cannot start Safari mode. Please save your game.")
        assert_boxes_or_party_can_fit_pokemon()
        assert_boxes_or_party_can_fit_pokemon(check_in_saved_game=True)
        assert_saved_on_map(SavedMapLocation(self._safari_config["map"]), self._safari_config["save_message"])

        while True:
            target_name = self._get_next_target(context.stats.get_global_stats().to_dict())
            if not target_name:
                context.message = "All required Pokémon caught. Safari is complete."
                context.set_manual_mode()
                break

            context.message = f"Current target: {target_name}"
            target = get_safari_pokemon(target_name)
            self._check_mode_requirement(target.value.mode, target.value.hunting_object)
            yield from self._check_map_requirement(target.value.map_location)

            if self._should_reset:
                if not self._atleast_one_pokemon_catched:
                    yield from self._soft_reset()
                    self._starting_cash = get_player().money
                else:
                    if is_safari_map():
                        yield from self._exit_safari_zone()
                    context.message = f"You hit the {self._money_spent_limit}₰ threshold, but caught at least one Pokémon. Consider saving."
                    context.set_manual_mode()
                    break
            elif self._should_reenter:
                yield from self._re_enter_safari_zone()

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

            self._use_repel = mode == "Use Repel"

            self._feeder_direction = RSESafariStrategy.get_facing_direction_for_position(target.value.tile_location)
            yield from self._start_safari_hunt(target)

    def _start_safari_hunt(self, safari_pokemon: SafariPokemon) -> Generator:
        if get_player().money < 500:
            raise BotModeError("Not enough cash to enter Safari Zone.")

        yield from navigate_to(self._safari_config["map"], self._safari_config["entrance_tile"])
        yield from ensure_facing_direction(self._safari_config["facing_direction"])

        context.emulator.hold_button(self._safari_config["facing_direction"])
        for _ in range(10):
            yield
        context.emulator.release_button(self._safari_config["facing_direction"])
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

    def _navigate_and_hunt(self, target_map, tile_location, mode) -> Generator:
        def stop():
            return self._should_reset or self._should_reenter

        if self._safari_config.get("is_at_entrance_door")():
            yield from wait_for_player_avatar_to_be_standing_still()
        elif context.rom.is_rse and self._safari_config.get("is_script_active", lambda: False)():
            while self._safari_config["is_at_entrance_door"]() or self._safari_config["is_script_active"]():
                yield
            yield from wait_for_player_avatar_to_be_standing_still()

        for map_group, coords in get_navigation_path(target_map, tile_location):
            self._current_map, self._current_tile = map_group, coords
            yield from navigate_to(map_group, coords)

        _, pokeblock = get_lowest_feel_pokeblock_by_type(self._pokeblock_type_in_feeder)
        yield from ensure_facing_direction(self._feeder_direction)
        yield from put_pokeblock_in_feeder(pokeblock)

        if self._use_repel and not repel_is_active():
            yield from apply_repel()

        delay_hours = random.uniform(1, 24)
        self._easter_egg_timestamp = datetime.now() + timedelta(hours=delay_hours)

        #         Testing
        self._easter_egg_timestamp = datetime.now() + timedelta(minutes=random.uniform(1, 2))
        print(self._easter_egg_timestamp)

        yield from unmount_bicycle()
        yield from apply_white_flute_if_available()
        yield from spin(
            stop_condition=stop,
            easter_egg_flag=lambda: self._do_easter_egg_after_battle,
            easter_egg_action=lambda: self._easter_egg_rest(self._current_map, self._current_tile),
            easter_egg_flag_setter=lambda val: setattr(self, "_do_easter_egg_after_battle", val),
        )

    def _re_enter_safari_zone(self) -> Generator:
        if is_safari_map():
            yield from self._exit_safari_zone()
        if self._should_save:
            yield from save_the_game()
        self._should_reenter = False
        self._should_save = False

    def _exit_safari_zone(self) -> Generator:
        yield from StartMenuNavigator("RETIRE").step()
        yield from wait_for_script_to_start_and_finish(self._safari_config["exit_script"], "A")
        yield from wait_for_player_avatar_to_be_standing_still()

    def _check_mode_requirement(self, mode, obj) -> bool:
        if mode == SafariHuntingMode.SURF:
            if not (get_event_flag("BADGE05_GET") and get_party().has_pokemon_with_move("Surf")):
                raise BotModeError("Missing Badge 05 or Surf move for surfing hunt.")
        elif mode == SafariHuntingMode.FISHING:
            assert_item_exists_in_bag(obj, f"You need the {obj} to fish.", check_in_saved_game=True)
        return True

    def _check_map_requirement(self, map) -> Generator:
        if map == MapRSE.SAFARI_ZONE_NORTHWEST:
            assert_item_exists_in_bag("Mach Bike", "Need Mach Bike for Northwest zone.", check_in_saved_game=True)
        elif map == MapRSE.SAFARI_ZONE_NORTH:
            try:
                assert_item_exists_in_bag("Acro Bike", check_in_saved_game=True)
            except BotModeError:
                yield from self._change_bike()

    def _soft_reset(self) -> Generator:
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

    def _easter_egg_rest(self, return_map_group, return_coords) -> Generator:
        yield from navigate_to(MapRSE.SAFARI_ZONE_SOUTHWEST, (32, 8))
        yield from ensure_facing_direction("Up")
        context.emulator.press_button("A")
        for _ in range(240):
            yield
        context.emulator.press_button("A")
        for _ in range(120):
            yield
        yield from ensure_facing_direction("Down")
        for _ in range(120):
            yield
        yield from ensure_facing_direction("Right")
        for _ in range(120):
            yield
        yield from ensure_facing_direction("Left")
        for _ in range(120):
            yield
        yield from ensure_facing_direction("Down")
        for _ in range(240):
            yield
        yield from navigate_to(MapRSE.SAFARI_ZONE_SOUTHWEST, (29, 7))
        yield from navigate_to(MapRSE.SAFARI_ZONE_REST_HOUSE, (2, 6))
        yield from ensure_facing_direction("Right")
        for _ in range(600):
            yield
        yield from navigate_to(MapRSE.SAFARI_ZONE_REST_HOUSE, (3, 8))
        yield from navigate_to(return_map_group, return_coords)
        yield from unmount_bicycle()
        yield from ensure_facing_direction(self._feeder_direction)
        if get_active_pokeblock_feeder_for_location() is None:
            index, pokeblock = get_lowest_feel_pokeblock_by_type(self._pokeblock_type_in_feeder)
            yield from put_pokeblock_in_feeder(pokeblock)
        yield from apply_white_flute_if_available()

    def _has_bike(self, name: str) -> bool:
        return get_item_bag().quantity_of(get_item_by_name(name)) > 0

    def _get_next_target(self, stats: dict) -> str | None:
        def get_catches(name: str) -> int:
            return stats.get("pokemon", {}).get(name, {}).get("catches", 0)

        zones = [
            ("southwest", {"core": {"Psyduck": 2, "Girafarig": 1, "Pikachu": 2}, "combo": None}),
            ("northwest", {"core": {"Rhyhorn": 2, "Pinsir": 1}, "combo": ("Doduo", "Dodrio")}),
            ("northeast", {"core": {"Phanpy": 2, "Heracross": 1}, "combo": ("Natu", "Xatu")}),
        ]

        for _, config in zones:
            missing = [name for name, req in config["core"].items() if get_catches(name) < req]
            if missing:
                return missing[0]
            if config["combo"]:
                a, b = config["combo"]
                if get_catches(a) < 2 and not (get_catches(a) >= 1 and get_catches(b) >= 1):
                    return a
        return None
