# Multicam Flow

Version 1.0

**Sync and cut multicam wedding footage for Final Cut Pro. Automatically.**

Drop in the folder of raw camera and audio files from a wedding. Multicam Flow lines up every camera and mic by sound, finds every stretch where two or more cameras were rolling (ceremony, reception, first looks, letter readings), builds a synced multicam for each, and cuts a first draft the way a wedding editor would:

- Holds on whoever is talking. Cuts to the couple's laughs and reactions.
- Fast cuts for the processional and entrances, favoring the roaming camera.
- Skips soft, shaky or blocked shots (someone walking in front of the lens).
- Cuts before a gimbal move starts, not halfway through it.
- Keeps it moving: a long hold gives way to another good angle, and smooth gimbal moves get worked in.

You get a normal Final Cut project with real multicam clips. Every cut is just an angle switch, so you can change any of them in seconds.

Everything runs on your Mac. No account, no upload, no subscription.

---

## System requirements

| | Minimum | Recommended |
|---|---|---|
| **Mac** | Any Mac that runs macOS 12 | Apple silicon (M1 or newer). Intel works but is slower. |
| **macOS** | 12 Monterey | 15 Sequoia or newer (adds Apple's shot-quality scoring) |
| **Memory** | 8 GB | 16 GB or more for long receptions |
| **Free disk space** | 3 GB for setup | Plus about 100 MB of working files per hour of multicam |
| **Final Cut Pro** | 10.6 | 11 or newer |
| **Python** | 3.9 or newer | Apple's Command Line Tools (setup offers to install them) or python.org |
| **Internet** | Needed once, for setup (about 1 GB download) | Not needed after that |

**Your footage needs:**

- **Sound on every camera.** Syncing works by matching sound, like Final Cut's "sync by audio." Scratch audio from the camera mic is fine.
- **Two or more cameras rolling at the same time** for anything you want as a multicam.
- **Video:** .mp4, .mov, .mxf, .mts, .m2ts, .avi, .m4v
- **Audio:** .wav, .mp3, .m4a, .aif, .flac (lav recorders, DJ board feeds, Zoom/Tascam recorders)

Mac only for now. Windows is not supported.

---

## Install

1. **Download** `Multicam-Flow-1.0.dmg` from the Releases page and open it.
2. **Drag Multicam Flow onto the Applications folder.**
3. **Open Multicam Flow** from Applications. The first time, macOS checks with you because the app isn't from the App Store:
   - **macOS 15 Sequoia or newer:** macOS says it can't verify the app. Click **Done**. Then open **System Settings > Privacy & Security**, scroll down to **Security**, and click **Open Anyway** next to Multicam Flow. Click **Open Anyway** again and enter your Mac password.
   - **macOS 14 Sonoma or older:** right-click (Control-click) the app, choose **Open**, then click **Open**.

   You only do this once. After that it opens with a normal double-click.
4. **Let it set up.** The first launch downloads what it needs (about 1 GB) and takes 5 to 10 minutes. A small window shows the progress.
   - If a window asks to install Apple's **command line developer tools**, click **Install**, wait for it to finish, then open Multicam Flow again.
5. Multicam Flow opens in your web browser. **Keep the small Multicam Flow window open while you work**, and quit it when you're done.

---

## How to use it

### A. Start from a folder of raw files (sync and cut)

1. Click **Choose wedding folder...** and pick the folder you offloaded the cards into.
2. Wait for the scan. The first scan reads every file off the drive, which takes roughly 10 to 30 minutes per wedding depending on your drive. Scanning the same wedding again is quick.
3. You'll see every **moment** where two or more cameras rolled together. For each one:
   - **Check** the ones you want, and **name** them anything: Ceremony, Reception, Dad first look, Letter reading. Moments with the same name become one multicam (a reception split by dinner stays one reception).
   - Pick a **style**. **Ceremony style** holds on whoever is talking and cuts to reactions. **Party style** cuts faster and favors the moving camera.
   - Check the **camera guesses** under each thumbnail (Roaming, Wide). Fix only the wrong ones, or leave them all. Setting "Bride's face", "Groom's face" or "Couple" helps it follow the vows and reactions.
4. Leave **Cut automatically after syncing** checked and click **Sync and cut**. You can walk away; your Mac stays awake until it's done. A 30-minute ceremony takes about 15 minutes on Apple silicon.
5. Click **Open in Final Cut**. Your finished files are also in **Movies/Multicam Flow/[folder name]**. You can drag them onto Final Cut too.
   - The main files are the cut projects.
   - **Synced only** holds the same multicams with no cut, if you'd rather edit from scratch.

The audio track is picked for you: the DJ board recording if there is one, then the officiant or a lav, then a camera. You can switch it in Final Cut with **Clip > Active Audio Angle**.

### B. Recut a project you already built in Final Cut

Use this when you've already made the multicam and have titles, graphics, adjustment layers or a logo around it.

1. In Final Cut, select the project and choose **File > Export XML**. Save it in Downloads, Desktop or Movies.
2. In Multicam Flow, pick it from the list under **Pick your export**.
3. Answer the questions: main camera (optional), which audio to keep, camera roles, and whether to cut out stretches where no camera has video.
4. When it's done, click **Open in Final Cut**. You get a copy of your project with only the multicam clips recut. Titles, graphics, adjustment layers, music and transitions stay exactly where they were.

---

## Get the best results

**Organize your card offload like this.** Folder names are how Multicam Flow knows which files belong to which camera and mic:

```
Smith Wedding/
  Video/
    Camera 1/      (any card folders inside are fine: DCIM, PRIVATE, CLIP...)
    Camera 2/
    Gimbal/
    Drone/
  Audio/
    DJ/
    Officiant/
    Groom/
    Lapel 1/
```

- **Name audio folders for what they are.** "DJ", "Board" or "Mixer" for the DJ feed. "Officiant", "Groom", "Bride", "Lav", "Lapel" or "Wireless" for lav mics. Lav names help it tell who is talking.
- **Keep every camera's clock roughly right.** It doesn't have to be exact (sync is by sound), but sorting the day into moments starts from recording times.
- **Recorder twins are handled.** Backup files like _D (safety track) or _MIX / _3 (DJ boards) get their own clearly named angles instead of looking like duplicates.
- **Exports, music and photos are ignored.** Folders named Project Files, Exports, Finals, Music, SFX or Photos are skipped, so a finished film never gets mistaken for a camera.

**It's a first draft, not a final edit.** Expect to fix a handful of cuts. It doesn't know which moments matter most to your couple.

---

## Troubleshooting

**macOS says it can't verify Multicam Flow, or that it "can't be opened."**
That's macOS checking an app that isn't from the App Store. Follow step 3 of Install above. On macOS 15 or newer the **Open Anyway** button in Privacy & Security only shows for about an hour after you try to open the app, so try opening it again first if you don't see it.

**Setup failed.**
Check your internet connection and open Multicam Flow again. Setup picks up where it stopped. Details are in `~/Library/Application Support/Multicam Flow/setup.log`. To start setup completely fresh, delete the `python` folder in that same place.

**Progress stops moving for a long time.**
Make sure your footage drive is still connected and your Mac didn't go to sleep with the lid closed. Click Quit, open Multicam Flow again, and run it again. Finished steps (the sound matching, and any moment already analyzed) are saved and reused; the moment it was working on starts over.

**A camera shows up blank in Final Cut.**
Delete the event you imported, then import the file again. If it's still blank, use File > Relink Files in Final Cut to point it at the footage (for example, if the drive has a different name now).

**Some clips are listed as "placed by the camera's clock."**
Those clips had nothing to match by sound: quiet b-roll, or a drone with no audio. They're placed by recording time, usually within a second. Give them a quick look in Final Cut.

**The browser window doesn't open, or you closed it.**
Open Multicam Flow from Applications again. It brings the window back.

---

## Privacy

Multicam Flow runs entirely on your Mac. Your footage never leaves your computer. The window is a local web page, only reachable from your Mac. The only internet use is the one-time setup download.

## Uninstall

Drag Multicam Flow from Applications to the Trash, and delete the folder `~/Library/Application Support/Multicam Flow` (in Finder: Go > Go to Folder, paste that path). Your finished files stay in Movies/Multicam Flow until you delete them too.

---

## Advanced

- **Settings:** `~/Library/Application Support/Multicam Flow/settings.txt` sets the defaults for recutting exports: shortest shot, longest shot, how loud a laugh has to be.
- **Working files** (analysis caches, logs) live in `~/Library/Application Support/Multicam Flow/Work`. Delete that folder anytime to free space. The next run on that footage just takes longer.
- **Command line:** from that same folder, `python/bin/python "/Applications/Multicam Flow.app/Contents/Resources/app/engine.py" --help` lists every option, including `--style party`, `--min-shot`, `--max-shot` and `--sheets` (writes contact sheets showing every camera and the cut, for reviewing decisions).
- **Building from source:** the `app` folder is the whole program. `Build Release.command` in the release kit wraps it into the Mac app and signs it.

## Credits

The Mac app is built with [Platypus](https://sveinbjorn.org/platypus) (BSD license). Built with [OpenCV](https://opencv.org) (image analysis), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) running OpenAI's Whisper speech model (transcripts), [FFmpeg](https://ffmpeg.org) via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (decoding), [NumPy](https://numpy.org), and Apple's Vision framework via [PyObjC](https://pyobjc.readthedocs.io). These are downloaded during setup under their own licenses. The face detection model file (`haarcascade_frontalface_default.xml`) comes from OpenCV and keeps its original license header.

Made by Ryan Abbott, Abbott Photo and Film, Fresno, California. Released under the MIT License (see LICENSE).
