
from ._interface import BotMode
from modules.context import context

class SeedFinderMode(BotMode):
    @staticmethod
    def is_selectable() -> bool:
        return True

    def name():
        return "SeedFinder"

    def run(self):
        print("SeedFinderMode: Simulating soft reset via Start+Select+B...")
        context.emulator.press_button("Start")
        context.emulator.press_button("Select")
        context.emulator.press_button("B")

        for _ in range(10):
            yield  # hold combo briefly

            context.emulator.reset_held_buttons()
        for _ in range(30):
            yield  # wait for boot

            print("Waiting for exactly 3 button presses...")
        presses = 0
        while presses < 2:
            if any(context.controller_state.values()):
                presses += 1
                print(f"Detected press {presses}/3")
                while any(context.controller_state.values()):
                    yield
            yield

        print("Ready. Waiting for 3rd press...")

        while True:
            if any(context.controller_state.values()):
                print("Saving state just before 3rd press...")
                context.emulator.save_state("target_third_press.sav")
                break
            yield

        print("Executing 3rd press")
        yield  # simulate press

        # Placeholder seed logic
        seed = (123456789 * 0x41C64E6D + 0x6073) & 0xFFFFFFFF
        upper16 = (seed >> 16) & 0xFFFF
        print(f"Observed seed: 0x{upper16:04X}")

        desired_seed = 0xABCD
        if upper16 == desired_seed:
            print("Success! Desired seed matched.")
            while True:
                yield

                print("Seed not matched. Beginning retry loop.")
        attempt = 1
        while True:
            context.emulator.load_state("target_third_press.sav")
            yield
            context.emulator.advance_frame()
            yield
            context.emulator.save_state("target_third_press.sav")
            yield

            print(f"[Attempt {attempt}] Executing 3rd press")
            yield  # simulate press frame

            seed = (seed * 0x41C64E6D + 0x6073) & 0xFFFFFFFF
            upper16 = (seed >> 16) & 0xFFFF
            print(f"Observed seed: 0x{upper16:04X}")

            if upper16 == desired_seed:
                print(f"Success after {attempt} attempts! Seed = 0x{upper16:04X}")
                while True:
                    yield

                    attempt += 1
            yield
