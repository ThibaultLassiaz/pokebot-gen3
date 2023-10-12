import atexit
import PIL.Image
import PIL.PngImagePlugin
import time
import zlib
from collections import deque
from typing import Union

import sounddevice

import mgba.audio
import mgba.core
import mgba.gba
import mgba.image
import mgba.log
import mgba.png
import mgba.vfs
from mgba import ffi, lib, libmgba_version_string
from modules.Console import console
from modules.Profiles import Profile


class PerformanceTracker:
    """
    This is a little helper utility used for measuring the FPS rate and allowing
    the emulator to calculate how long it needs to wait to hit a targeted FPS rate.
    """
    last_render_time: int = 0
    last_frame_time: int = 0

    fps_history: deque[int] = deque([0], maxlen=60)
    frame_counter: int = 0
    frame_counter_time: int = 0

    time_spent_in_bot_fraction_history: deque[float] = deque([0.0], maxlen=60)
    time_spent_emulating: int = 0
    time_spent_total: int = 0

    def TrackRender(self) -> None:
        self.last_render_time = time.time_ns()

    def TimeSinceLastRender(self) -> int:
        return time.time_ns() - self.last_render_time

    def TrackFrame(self) -> None:
        now = time.time_ns()
        self.time_spent_total += now - self.last_frame_time
        self.last_frame_time = now
        current_second = int(time.time())
        if self.frame_counter_time != current_second:
            self.fps_history.append(self.frame_counter)
            self.frame_counter = 0
            self.frame_counter_time = current_second

            if self.time_spent_total > 0:
                time_spent_in_bot = self.time_spent_total - self.time_spent_emulating
                self.time_spent_in_bot_fraction_history.append(time_spent_in_bot / self.time_spent_total)

            self.time_spent_total = 0
            self.time_spent_emulating = 0

        self.frame_counter += 1

    def TimeSinceLastFrame(self) -> int:
        return time.time_ns() - self.last_frame_time


class LibmgbaEmulator:
    """
    This class wraps libmgba and handles the actual emulation of a game, and exposes some of the
    emulator's functions (such as memory access and I/O)
    """

    _video_enabled: bool = True
    _audio_enabled: bool = True
    _throttled: bool = False
    _speed_factor: float = 1
    # How often a frame should be drawn to the screen (can be less frequent than the emulation rate)
    _target_seconds_per_render = 1 / 60

    _audio_stream: Union[sounddevice.RawOutputStream, None] = None

    def __init__(self, profile: Profile, on_frame_callback: callable):
        console.print(f'Running [cyan]{libmgba_version_string()}[/]')

        # Prevents relentless spamming to stdout by libmgba.
        mgba.log.silence()

        self._profile = profile
        self._core = mgba.core.load_path(str(profile.rom.file))
        if not self._core:
            raise RuntimeError(f'Could not load ROM file {str(profile.rom.file)}')

        # libmgba needs a save file to be loaded, or otherwise it will not save anything
        # to disk if the player saves the game. This can be an empty file.
        self._current_save_path = profile.path / 'current_save.sav'
        if not self._current_save_path.exists():
            # Create an empty file if a save game does not exist yet.
            with open(self._current_save_path, 'wb'):
                pass
        self._save = mgba.vfs.open_path(str(self._current_save_path), 'r+')
        self._core.load_save(self._save)

        self._screen = mgba.image.Image(*self._core.desired_video_dimensions())
        self._core.set_video_buffer(self._screen)
        self._core.reset()

        # Whenever the emulator closes, it stores the current state in `current_state.ss1`.
        # Load this file if it exists, to continue exactly where we left off.
        self._current_state_path = profile.path / 'current_state.ss1'
        if self._current_state_path.exists():
            with open(self._current_state_path, 'rb') as state_file:
                self.LoadSaveState(state_file.read())

        self._memory: mgba.gba.GBAMemory = self._core.memory
        self._on_frame_callback = on_frame_callback
        self._performance_tracker = PerformanceTracker()

        self._gba_audio = self._core.get_audio_channels()
        self._ResetAudio()

        atexit.register(self.Shutdown)
        self._core._callbacks.savedata_updated.append(self.BackupCurrentSaveGame)

    def _ResetAudio(self) -> None:
        """
        This is called at the start of the emulation, as well as when there has been
        an error during playback. The latter can happen if the audio device suddenly
        disappears (e.g. a USB headset being unplugged.)

        In that case, we will attempt to re-start the audio stream (which should then
        use the new default device) or, if that fails, disable audio playback entirely.
        """
        if self._audio_stream is not None:
            self._audio_stream.stop(ignore_errors=True)
            self._audio_stream.close(ignore_errors=True)
            del self._audio_stream

        try:
            default_sound_device = sounddevice.query_devices(device=sounddevice.default.device, kind='output')
            sample_rate = int(default_sound_device['default_samplerate'])

            if default_sound_device['max_output_channels'] < 2:
                raise sounddevice.PortAudioError('Your audio device does not support stereo. '
                                                 'What on earth are you using, a yoghurt pot telephone?!')

            self._gba_audio.set_rate(sample_rate)
            self._audio_stream = sounddevice.RawOutputStream(channels=2, samplerate=sample_rate, dtype='int16')
            if self._throttled:
                self._audio_stream.start()
        except sounddevice.PortAudioError as error:
            console.print(f'[red]{str(error)}[/]')
            console.print('[red bold]Failed to initialise sound![/] [red]Sound will be disabled.[/]')
            self._audio_stream = None

    def Reset(self) -> None:
        self._core.reset()

    def CreateSaveState(self, suffix: str = '') -> None:
        states_directory = self._profile.path / 'states'
        if not states_directory.exists():
            states_directory.mkdir()

        screenshot = self.GetScreenshot()
        extra_chunks = PIL.PngImagePlugin.PngInfo()
        extra_chunks.add(b'gbAs', zlib.compress(self.GetSaveState()))

        # First, we store the current state as a new file inside the `states/` directory -- so that in case
        # anything goes wrong here (full disk or whatever) we catch it before overriding the current state.
        # This also serves as a backup directory -- in case the bot does something dumb, the user can just
        # restore one of the states from this directory.
        filename = time.strftime('%Y-%m-%d_%H-%M-%S')
        if suffix:
            filename += f'_{suffix}'
        filename += '.ss1'
        backup_path = states_directory / filename
        with open(backup_path, 'wb') as state_file:
            screenshot.save(state_file, format='PNG', pnginfo=extra_chunks)

        console.print(f'Save state {backup_path} created!')

        # Once that succeeds, override `current_state.ss1` (which is what the bot loads on startup.)
        if backup_path.stat().st_size > 0:
            with open(self._current_state_path, 'wb') as state_file:
                screenshot.save(state_file, format='PNG', pnginfo=extra_chunks)

        console.print(f'Updated `current_state.ss1`!')

    def Shutdown(self) -> None:
        """
        This method is called whenever the bot shuts down, either because and error occurred or because
        the user initiated the exit (by pressing Escape or closing the window.)

        It's saving the current emulator state into a save state file so that the next time the bot starts,
        it can just continue where it has been so rudely interrupted.
        """
        console.print('[yellow]Shutting down...[/]')

        self.CreateSaveState()

    def BackupCurrentSaveGame(self) -> None:
        """
        This is called every time the emulator writes out the save game (which happens when the player
        uses the 'Save' function in-game.)

        For backup purposes, we keep a copy of every save ever created and put it inside the profile's
        `saves/` directory.
        """
        with open(self._current_save_path, 'rb') as save_file:
            saves_directory = self._profile.path / 'saves'
            if not saves_directory.exists():
                saves_directory.mkdir()
            with open(saves_directory / time.strftime('%Y-%m-%d_%H-%M-%S.sav'), 'wb') as backup_file:
                backup_file.write(save_file.read())

    def GetFrameCount(self) -> int:
        """
        :return: The number of frames since the start of this emulation session
        """
        return self._core.frame_counter

    def GetImageDimensions(self) -> tuple[int, int]:
        """
        :return: The screen resolution (width, height) of the GBA
        """
        return self._core.desired_video_dimensions()

    def GetCurrentFPS(self) -> int:
        """
        :return: Number of frames emulated in the last second
        """
        return self._performance_tracker.fps_history[-1]

    def GetCurrentTimeSpentInBotFraction(self) -> float:
        """
        This indicates what fraction of time per frame has been spent in bot processing code (i.e. outside of this
        class) compared

        Note that this only compares bot processing time to emulation time, while entirely ignoring video rendering
        and audio output. This is so the number stays consistent whether or not throttling is enabled and video is
        turned on or off.

        But it means that this number does not necessarily reflect how much _actual_ time is being spent in bot code,
        just how it compares to mGBA's processing time. It is meant to serve as an indicator whether bot processing
        performance improved or worsened during development, rather than the absolute number being meaningful.

        :return: Fraction of time spent in bot processing code in the last second compared to emulation
        """
        return self._performance_tracker.time_spent_in_bot_fraction_history[-1]

    def GetVideoEnabled(self) -> bool:
        return self._video_enabled

    def SetVideoEnabled(self, video_enabled: bool) -> None:
        """
        Enable or disable video.

        Apart from preventing the image from being rendered to the GUI, this will also toggle between
        the 'real' and the dummy renderer inside libmgba. The latter will just ignore any command that
        has to do with rendering, vastly speeding up the emulation.

        This also means that taking screenshots is not possible while video is disabled.

        :param video_enabled: Whether video output is enabled or not.
        """
        self._video_enabled = video_enabled

        self._core._native.video.renderer.disableBG[0] = not video_enabled
        self._core._native.video.renderer.disableBG[1] = not video_enabled
        self._core._native.video.renderer.disableBG[2] = not video_enabled
        self._core._native.video.renderer.disableBG[3] = not video_enabled
        self._core._native.video.renderer.disableOBJ = not video_enabled

        self._core._native.video.renderer.disableWIN[0] = not video_enabled
        self._core._native.video.renderer.disableWIN[1] = not video_enabled
        self._core._native.video.renderer.disableOBJWIN = not video_enabled

    def GetAudioEnabled(self) -> bool:
        return self._audio_enabled

    def SetAudioEnabled(self, audio_enabled) -> None:
        self._audio_enabled = audio_enabled

    def GetThrottle(self) -> bool:
        """
        :return: Whether the emulator currently runs at 1× speed (True) or unthrottled (False)
        """
        return self._throttled

    def SetThrottle(self, is_throttled: bool) -> None:
        """
        :param is_throttled: True for 1× speed, False for unthrottled
        """
        was_throttled = self._throttled
        self._throttled = is_throttled

        if self._audio_stream is not None:
            if is_throttled and not was_throttled:
                self._audio_stream.start()
            elif not is_throttled and was_throttled:
                self._audio_stream.stop()

    def GetSpeedFactor(self) -> float:
        return self._speed_factor

    def SetSpeedFactor(self, speed_factor: float) -> None:
        self._speed_factor = speed_factor

        if self._audio_stream is not None:
            self._gba_audio.set_rate(self._audio_stream.samplerate // speed_factor)

    def GetSaveState(self) -> bytes:
        """
        Returns the current serialised emulator state (i.e. a save state in mGBA parlance)
        :return: The raw save state data
        """
        return self._core.save_state()

    def LoadSaveState(self, state: bytes) -> None:
        """
        Loads a serialised emulator state (i.e. a save state in mGBA parlance)
        :param state: The raw save state data
        """
        vfile = mgba.vfs.VFile.fromEmpty()
        vfile.write(state, len(state))
        vfile.seek(0, whence=0)
        self._core.load_state(vfile)

    def ReadBytes(self, address: int, length: int = 1) -> bytes:
        """
        Reads a block of memory from an arbitrary address on the system
        bus. That means that you need to specify the full memory address
        rather than an offset relative to the start of a given memory
        area.

        This is helpful if you are working with the symbol table or
        pointers.

        :param address: Full memory address to read from
        :param length: Number of bytes to read
        :return: Data read from that memory location
        """
        bank = address >> 0x18
        result = bytearray(length)
        if bank == 0x2:
            offset = address & 0x3FFFF
            if offset + length > 0x3FFFF:
                raise RuntimeError('Illegal range: EWRAM only extends from 0x02000000 to 0x0203FFFF')
            ffi.memmove(result, ffi.cast('char*', self._core._native.memory.wram) + offset, length)
        elif bank == 0x3:
            offset = address & 0x7FFF
            if offset + length > 0x7FFF:
                raise RuntimeError('Illegal range: IWRAM only extends from 0x03000000 to 0x03007FFF')
            ffi.memmove(result, ffi.cast('char*', self._core._native.memory.iwram) + offset, length)
        elif bank >= 0x8:
            offset = address - 0x08000000
            ffi.memmove(result, ffi.cast('char*', self._core._native.memory.rom) + offset, length)
        else:
            raise RuntimeError(f'Invalid memory address for reading: {hex(address)}')
        return result

    def WriteBytes(self, address: int, data: bytes) -> None:
        """
        Writes to an arbitrary address on the system bus.

        This only allows writing to the EWRAM (memory addresses starting with 0x02) and
        IWRAM (memory addresses starting with 0x03.)

        :param address: The full memory address to write to
        :param data: Data to write
        """
        bank = address >> 0x18
        length = len(data)
        if bank == 0x2:
            offset = address & 0x3FFFF
            if offset + length > 0x3FFFF:
                raise RuntimeError('Illegal range: EWRAM only extends from 0x02000000 to 0x0203FFFF')
            ffi.memmove(ffi.cast('char*', self._core._native.memory.wram) + offset, data, length)
        elif bank == 0x3:
            offset = address & 0x7FFF
            if offset + length > 0x7FFF:
                raise RuntimeError('Illegal range: IWRAM only extends from 0x03000000 to 0x03007FFF')
            ffi.memmove(ffi.cast('char*', self._core._native.memory.iwram) + offset, data, length)
        else:
            raise RuntimeError(f'Invalid memory address for writing: {hex(address)}')

    def GetInputs(self) -> int:
        """
        :return: A bitfield with all the buttons that are currently being pressed
        """
        return self._core._core.getKeys(self._core._core)

    def SetInputs(self, inputs: int):
        """
        :param inputs: A bitfield with all the buttons that should now be pressed
        """
        self._core._core.setKeys(self._core._core, inputs)

    def GetCurrentScreenImage(self) -> PIL.Image.Image:
        return self._screen.to_pil()

    def GetScreenshot(self) -> PIL.Image.Image:
        current_state = None
        if not self._video_enabled:
            # If video has been disabled, it's not possible to receive the current screen content
            # because mGBA never rendered it at all.
            # So as a workaround, we enable video and then emulate a single frame (this is necessary
            # for mGBA to update the screen.) In order to not mess up emulation and frame timing,
            # we take a save state before and then restore it afterwards.
            # So the screenshot will be 1 frame late, but the emulation will resume from the same
            # state.
            self.SetVideoEnabled(True)
            current_state = self.GetSaveState()
            self._core.run_frame()

        screenshot = self.GetCurrentScreenImage().convert('RGB')

        if current_state is not None:
            self.LoadSaveState(current_state)
            self.SetVideoEnabled(False)

        return screenshot

    def TakeScreenshot(self, suffix: str = '') -> None:
        """
        Saves the currently displayed image as a screenshot inside the profile's `screenshots/`
        directory.
        """
        png_directory = self._profile.path / 'screenshots'
        if not png_directory.exists():
            png_directory.mkdir()
        current_timestamp = time.strftime('%Y-%m-%d_%H-%M-%S')
        if suffix != '':
            suffix = f'_{suffix}'
        png_path = png_directory / f'{current_timestamp}_{str(self.GetFrameCount())}{suffix}.png'
        with open(png_path, 'wb') as file:
            self.GetScreenshot().save(file, format='PNG')

    def PeekFrame(self, callback: callable, frames_to_advance: int = 1) -> any:
        """
        Runs the emulation for a number of frames and then runs {callback()}, after which it restores
        the original emulator state.

        This can be used to check the emulator state in a given number of frames without actually
        advancing the emulation.

        :param callback: A function to run after the emulation has progressed
        :param frames_to_advance: Optional number of frames to advance (defaults to 1)
        :return: The return value of the callback function
        """
        original_emulator_state = self.GetSaveState()
        for i in range(frames_to_advance):
            self._core.run_frame()
        result = callback()
        self.LoadSaveState(original_emulator_state)
        return result

    def RunSingleFrame(self) -> None:
        """
        Runs the emulation for a single frame, and then waits if necessary to hit the target FPS rate.
        """
        begin = time.time_ns()
        self._core.run_frame()
        self._performance_tracker.time_spent_emulating += time.time_ns() - begin

        begin = time.time_ns()
        self._on_frame_callback()

        # Limiting FPS is achieved by using a blocking API for audio playback -- meaning we give it
        # the audio data for one frame and the `write()` call will only return once it processed the
        # data, effectively halting the bot until it's time for a new frame.
        #
        # Using speeds other than 1× is achieved by changing the GBA's sample rate. If the GBA only
        # produces half the amount of samples per frame, then the audio system will play them in half
        # the time of a frame, effectively giving us a 2× speed.
        #
        # This all depends on audio actually _working_, though. So in case audio could, for whatever
        # reason, not be initialised, we fall back to a sleep-based throttling mechanism. This is less
        # reliable, though, as the OS does not guarantee `sleep()` to return after the specified amount
        # of time. It just will sleep for _at least_ that time.
        if self._throttled:
            if self._audio_stream:
                samples_available = self._gba_audio.available
                audio_data = bytearray(samples_available * 4)
                try:
                    if self._audio_enabled:
                        ffi.memmove(audio_data, self._gba_audio.read(samples_available), len(audio_data))
                        self._audio_stream.write(audio_data)
                    else:
                        self._gba_audio.clear()
                        self._audio_stream.write(audio_data)
                except sounddevice.PortAudioError as error:
                    console.print(f'[bold red]Error while playing audio:[/] [red]{str(error)}[/]')
                    self._ResetAudio()
            else:
                target_frame_duration = (1 / 60) / self._speed_factor
                time_since_last_frame = self._performance_tracker.TimeSinceLastFrame() / 1_000_000_000
                if time_since_last_frame < target_frame_duration:
                    time.sleep(target_frame_duration - time_since_last_frame)

        self._performance_tracker.time_spent_total -= time.time_ns() - begin
        self._performance_tracker.TrackFrame()
