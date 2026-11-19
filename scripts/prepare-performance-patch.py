#!/usr/bin/env python3
"""Generate 0083 against pinned ios-sweet's four introduced driver files.

Fail on changed source anchors; never includes the untested GPI/touch lane.
Usage: prepare-performance-patch.py IOS_SWEET_PATCH_DIR OUTPUT.patch
"""
import difflib
from pathlib import Path
import subprocess
import sys
import tempfile

FB = 'iokit/Kernel/arm/SweetFramebuffer.cpp'
HID = 'iokit/Kernel/arm/hid/SweetHID.cpp'
KEYS = 'bsd/dev/sweet_keys.c'
USB = 'bsd/dev/sweet_usb.c'
SPMI = 'bsd/dev/sweet_spmi.c'
BATT = 'iokit/Kernel/arm/SweetBattery.cpp'
PATHS = (FB, HID, KEYS, USB, SPMI, BATT)

FADE_BLOCK = '''	if (fb->fPowerOff) {
		/* Fade out: halve every channel each vsync, then black, then idle. */
		uint32_t step = fb->fFadeStep;
		if (step <= 6) {
			fb->fFadeStep = step + 1;
		}
		IOLockUnlock(fb->fLock);
		if (step <= 6) {
			for (uint32_t y = 0; y < fb->fHeight; y++) {
				volatile uint32_t *row = (volatile uint32_t *)(fb->fPixels + (uint64_t)y * fb->fRowBytes);
				for (uint32_t x = 0; x < fb->fWidth; x++) {
					row[x] = step < 6 ? (row[x] >> 1) & 0x7f7f7f7f : 0;
				}
			}
		}
		sender->setTimeoutUS(SWEET_FB_VSYNC_US);
		return;
	}'''

BLANK_BLOCK = '''	if (fb->fPowerOff) {
		bool blank = !fb->fBlanked;
		uint64_t begin = 0;
		if (blank) {
			begin = mach_absolute_time();
			// Write-only clear: never read WC panel RAM for a fade effect.
			// Keep fLock held through this single clear so a concurrent on
			// request cannot be followed by a stale, delayed black write.
			bzero((void *)fb->fPixels, (size_t)fb->fRowBytes * fb->fHeight);
			fb->fBlanked = true;
		}
		IOLockUnlock(fb->fLock);
		if (blank) {
			uint64_t ns;
			absolutetime_to_nanoseconds(mach_absolute_time() - begin, &ns);
			IOLog("SWEET BLANK: clear_us=%llu bytes=%llu\\n", ns / 1000,
			    (uint64_t)fb->fRowBytes * fb->fHeight);
		}
		fb->finishTick(sender);
		return;
	}'''


def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError(f'upstream anchor count != {count}: {old[:100]!r}')
    return text.replace(old, new)


def transform(path, text):
    if path == HID:
        text = replace(text, '\tUInt32 fTestCount;',
                       '\tUInt32 fTestCount;\n\tUInt32 fTestOnce;\n\tUInt32 fTestWaits;')
        text = replace(text, '\tif (PE_parse_boot_argn("sweethidtest",',
                       '\tPE_parse_boot_argn("sweethidonce", &fTestOnce, sizeof(fTestOnce));\n'
                       '\tif (PE_parse_boot_argn("sweethidtest",')
        text = replace(text, '\tbool press = (self->fTestCount & 1) == 0;', '''
	// In once mode, wait for an HID consumer before the first press, bounded
	// to five minutes. A release is always sent even if the consumer closes.
	if (self->fTestOnce && self->fTestCount == 0 &&
	    (!self->_clients || self->_clients->getCount() == 0)) {
		if (++self->fTestWaits < 300) { sender->setTimeoutMS(1000); }
		return;
	}
	bool press = (self->fTestCount & 1) == 0;''')
        text = replace(text, '\tif (++self->fTestCount < 60) {',
                       '\tif (++self->fTestCount < (self->fTestOnce ? 2u : 60u)) {')
        text = replace(text, '\t\tsender->setTimeoutMS(press ? 100 : self->fTestSecs * 1000);\n\t}',
                       '\t\tsender->setTimeoutMS(press ? 100 : self->fTestSecs * 1000);\n'
                       '\t} else if (self->fTestOnce) {\n'
                       '\t\tIOLog("SWEET HID: one-shot Home complete\\n");\n\t}')
    elif path == KEYS:
        # The reference SID4 GPIO2 is not Mi 9's volume-up. Do not emit its
        # permanently-down event or read it in the performance control.
        # sweetvolup=PPID selects the board's volume-up PMIC GPIO peripheral
        # (PPID = SID << 8 | GPIO base >> 8, read-only like the reference).
        # Polling it from boot coincided with the ST-FTS self-sense
        # calibration failing on every test boot (43 05 30/31, then a silent
        # touch IC); without it touch came up. Start reading it only after
        # sweetvolupdelay seconds (default 150), past touch bring-up.
        text = replace(text, 'static bool keys_started;', 'static bool keys_started;\nstatic int keys_pon_only;\nstatic int keys_volup_ppid;\nstatic uint64_t keys_volup_after;')
        text = replace(text, '\t\tbool gok = keys_read(keys_gpio_apid, KEYS_GPIO_RT, &gpio);',
                       '\t\tuint16_t gapid = mach_absolute_time() >= keys_volup_after ? keys_gpio_apid : KEYS_NO_APID;\n'
                       '\t\tbool gok = keys_read(gapid, KEYS_GPIO_RT, &gpio);')
        text = replace(text, '\tkeys_gpio_apid = keys_find_apid(0x4c1, count);',
                       '\tPE_parse_boot_argn("sweetkeyspononly", &keys_pon_only, sizeof(keys_pon_only));\n'
                       '\tPE_parse_boot_argn("sweetvolup", &keys_volup_ppid, sizeof(keys_volup_ppid));\n'
                       '\tif (keys_volup_ppid > 0 && keys_volup_ppid < 0x1000) {\n'
                       '\t\tkeys_gpio_apid = keys_find_apid((uint16_t)keys_volup_ppid, count);\n'
                       '\t\tunsigned volup_delay = 150;\n'
                       '\t\tPE_parse_boot_argn("sweetvolupdelay", &volup_delay, sizeof(volup_delay));\n'
                       '\t\tclock_interval_to_deadline(volup_delay, NSEC_PER_SEC, &keys_volup_after);\n'
                       '\t\tprintf("SWEET KEYS: volume-up ppid=%#x apid=%u polled after %u s\\n", keys_volup_ppid, keys_gpio_apid, volup_delay);\n'
                       '\t} else {\n'
                       '\t\tkeys_gpio_apid = keys_pon_only ? KEYS_NO_APID : keys_find_apid(0x4c1, count);\n'
                       '\t}')
        text = replace(text, '\t\tif (!pok || !gok) {', '\t\tif (!pok || (!gok && gapid != KEYS_NO_APID)) {')
    elif path == SPMI:
        # Mi 9 battery lives on PM8150B (SID 2): SMB5 charger blocks share
        # the PM6150 layout; the gauge is FG gen4 (BATT_SOC 0x40, BATT_INFO
        # 0x41) instead of QG 0x48. Still observer reads only.
        text = replace(text, '''	if (!sp_ready || !out || sid != 0 || addr > 0xffffu ||
	    (peripheral != 0x10 && peripheral != 0x11 && peripheral != 0x12 &&
	    peripheral != 0x13 && peripheral != 0x48)) {''', '''	bool sweet_block = sid == 0 && (peripheral == 0x10 || peripheral == 0x11 ||
	    peripheral == 0x12 || peripheral == 0x13 || peripheral == 0x48);
	bool cepheus_block = sid == 2 && (peripheral == 0x10 || peripheral == 0x11 ||
	    peripheral == 0x13 || peripheral == 0x40 || peripheral == 0x41);
	if (!sp_ready || !out || addr > 0xffffu || (!sweet_block && !cepheus_block)) {''')
    elif path == BATT:
        text = replace(text, '\tvoid poll(void);',
                       '\tvoid poll(void);\n\tvoid pollCepheus(void);\n'
                       '\tint fMode;\n\tbool fArmed;\n\tUInt32 fDesign, fDelayMS;\n\tunsigned fLogs, fLastPercent;\n'
                       '\tstatic bool readPair(unsigned a, unsigned b, uint16_t *value);')
        text = replace(text, '\tfLastMode = -1;\n\treturn true;',
                       '\tfLastMode = -1;\n\tfMode = 0;\n\tfArmed = false;\n\tfDesign = 5020;\n'
                       '\tfDelayMS = 150000;\n\tfLogs = 0;\n\tfLastPercent = ~0u;\n\treturn true;')
        text = replace(text, '(5020u * percent + 50u)', '(fDesign * percent + 50u)')
        text = replace(text, '5020);', 'fDesign);', count=3)
        text = replace(text, '{capacity, 5020,', '{capacity, fDesign,')
        text = replace(text, '\tputBool("SweetBatterySOCEstimated", measured);',
                       '\tputBool("SweetBatterySOCEstimated", measured && fMode != 2);')
        text = replace(text, 'measured ? "PM6150 / estimated SOC" : "static fallback"',
                       'measured ? (fMode == 2 ? "PM8150B FG gen4 SOC" : "PM6150 / estimated SOC") : "static fallback"')
        text = replace(text, 'void\nSweetBattery::poll(void)\n{\n', '''/* FG gen4 keeps a copy (_CP) register next to each sample; Linux accepts a
 * sample only when the pair matches. Same here, a few bounded tries. */
bool
SweetBattery::readPair(unsigned a, unsigned b, uint16_t *value)
{
	for (unsigned attempt = 0; attempt < 4; ++attempt) {
		uint8_t lo, hi, clo, chi;
		if (sweet_spmi_read(2, a, &lo) || sweet_spmi_read(2, a + 1, &hi) ||
		    sweet_spmi_read(2, b, &clo) || sweet_spmi_read(2, b + 1, &chi)) {
			return false;
		}
		if (lo == clo && hi == chi) {
			*value = (uint16_t)((unsigned)lo | ((unsigned)hi << 8));
			return true;
		}
	}
	return false;
}

/* sweetbattery=2: Mi 9 PM8150B. MSOC/VBATT/IBATT decode follows Linux
 * fg-util.c (fg_get_msoc, fg_get_battery_voltage/current); charger state
 * uses the SMB5 registers of the reference path on SID 2. Read only.
 * The first hardware read waits sweetbattdelay seconds (default 150), past
 * touch calibration, like the volume-up poll. */
void
SweetBattery::pollCepheus(void)
{
	uint8_t soc = 0, socCopy = 1, usb, path, charger, error, temperature;
	bool socOk = false;
	if (!fArmed) {
		publish(100, 4200, 0, true, true, false, true, false, false);
		return;
	}
	for (unsigned attempt = 0; attempt < 4 && !socOk; ++attempt) {
		if (sweet_spmi_read(2, 0x4009, &soc) || sweet_spmi_read(2, 0x400a, &socCopy)) {
			break;
		}
		socOk = soc == socCopy;
	}
	bool valid = socOk &&
	    !sweet_spmi_read(2, 0x1310, &usb) &&
	    !sweet_spmi_read(2, 0x110b, &path) &&
	    !sweet_spmi_read(2, 0x1006, &charger) &&
	    !sweet_spmi_read(2, 0x1007, &error) &&
	    !sweet_spmi_read(2, 0x100d, &temperature);
	if (!valid) {
		if (fLogs < 8) {
			++fLogs;
			IOLog("CEPHEUS BATTERY: read failed soc=%#x/%#x ok=%d\\n", soc, socCopy, socOk);
		}
		publish(100, 4200, 0, true, true, false, true, false, false);
		return;
	}
	unsigned percent = soc == 255 ? 100u : soc == 0 ? 0u :
	    ((unsigned)(soc - 1) * 98u + 126u) / 253u + 1u;
	uint16_t rawVoltage = 0, rawCurrent = 0;
	unsigned mv = readPair(0x41a0, 0x41a6, &rawVoltage) ?
	    (unsigned)(((uint64_t)rawVoltage * 122070u) / 1000000u) : 0;
	if (mv < 2800 || mv > 4600) {
		mv = 3800; /* Display only; SOC comes from the gauge, not voltage. */
	}
	bool currentValid = readPair(0x41a2, 0x41a8, &rawCurrent);
	int ma = 0;
	if (currentValid) {
		int signedRaw = (rawCurrent & 0x8000u) ? (int)rawCurrent - 65536 : (int)rawCurrent;
		/* Linux: positive=discharging; IOPMPowerSource: positive=charging. */
		ma = (int)(-((int64_t)signedRaw * 488281) / 1000000);
	}
	bool external = (usb & 0x10u) != 0;
	bool capable = external && (path & 0x10u) && (path & 1u) && !(path & 0x40u);
	unsigned state = charger & 7u;
	bool charging = capable && state >= 1 && state <= 4 &&
	    !(error & 2u) && !(temperature & 0x0cu);
	bool full = external && state == 5 && !(error & 2u);
	if (full) {
		percent = 100;
	}
	if (percent != fLastPercent && fLogs < 64) {
		++fLogs;
		IOLog("CEPHEUS BATTERY: msoc=%#x %u%% %umV %dmA usb=%#x path=%#x chg=%#x err=%#x\\n",
		    soc, percent, mv, ma, usb, path, charger, error);
	}
	fLastPercent = percent;
	publish(percent, mv, ma, external, capable, charging, full, true, currentValid);
}

void
SweetBattery::poll(void)
{
	if (fMode == 2) {
		pollCepheus();
		return;
	}
''')
        text = replace(text, '\tif (self && self->fRunning) {\n\t\tself->poll();',
                       '\tif (self && self->fRunning) {\n\t\tself->fArmed = true;\n\t\tself->poll();')
        text = replace(text, '\tfReal = real == 1 && sweet_spmi_init() == 0;', '''	fMode = real;
	if (real == 2) {
		unsigned delaySecs = 150;
		(void)PE_parse_boot_argn("sweetbattdelay", &delaySecs, sizeof(delaySecs));
		fDelayMS = (delaySecs > 3600 ? 3600 : (delaySecs ? delaySecs : 1)) * 1000;
		fDesign = 3300; /* Mi 9 nominal cell */
	}
	fReal = (real == 1 || real == 2) && sweet_spmi_init() == 0;''')
        text = replace(text, '\t\t\tfTimer->setTimeoutMS(30000);',
                       '\t\t\tfTimer->setTimeoutMS(fMode == 2 ? fDelayMS : 30000);')
    elif path == USB:
        text = replace(text, '\tprintf("SWEET USB: setup %02x %02x %04x %04x len %u\\n", type, req, value, index, length);', '''
	// Keep handling each request; only suppress repeated filter trace lines.
	static unsigned filter_traces;
	if (type != 0x21 || req != 0x43 || filter_traces++ < 4) {
		printf("SWEET USB: setup %02x %02x %04x %04x len %u\\n", type, req, value, index, length);
	}''')
    elif path == FB:
        text = replace(text, '\tuint32_t      fFadeStep;      /* fade-out steps done while off */', '''
	bool          fBlanked;
	uint8_t       fPresentRecord[SWEET_FB_SWAP_REC_SIZE]; // workloop-owned
	uint64_t      fPeriod, fDeadline, fStatsPeriod, fStatsAt;
	uint64_t      fSubmitted, fOverwritten, fPresented, fCopyTicks, fCopyMax;
	uint64_t      fCopiedBytes, fMissed;
	uint32_t      fFixedTimer, fPerfStats;
	void finishTick(IOTimerEventSource *sender);''')
        text = replace(text, FADE_BLOCK, BLANK_BLOCK)
        text = replace(text, '\t\t\tfFB->fFadeStep = 0;', '\t\t\tfFB->fBlanked = false;')
        text = replace(text, 'Off fades the panel out over a few\n *      vsyncs (halving each pixel)',
                       'Off clears the panel once with stores only\n *      (no slow read/modify/write of WC memory)')
        text = replace(text, '\tfVsync->setTimeoutUS(SWEET_FB_VSYNC_US);', '''
	PE_parse_boot_argn("sweetfb_fixedtimer", &fFixedTimer, sizeof(fFixedTimer));
	PE_parse_boot_argn("sweetfb_stats", &fPerfStats, sizeof(fPerfStats));
	clock_interval_to_absolutetime_interval(SWEET_FB_VSYNC_US, NSEC_PER_USEC, &fPeriod);
	clock_interval_to_absolutetime_interval(10, NSEC_PER_SEC, &fStatsPeriod);
	fStatsAt = mach_absolute_time();
	fDeadline = fStatsAt + fPeriod;
	fVsync->wakeAtTime(fDeadline);''')
        text = replace(text, '''	if (flush) {
		rec = (uint8_t *)IOMallocData(SWEET_FB_SWAP_REC_SIZE);
		if (rec != NULL) {
			memcpy(rec, fb->fPending, SWEET_FB_SWAP_REC_SIZE);
			fb->fDirty = false;
		}
	}''', '''	if (flush) {
		rec = fb->fPresentRecord;
		memcpy(rec, fb->fPending, SWEET_FB_SWAP_REC_SIZE);
		fb->fDirty = false;
	}''')
        text = replace(text, '''	if (rec != NULL) {
		fb->present(rec);
		IOFreeData(rec, SWEET_FB_SWAP_REC_SIZE);
	}
	sender->setTimeoutUS(SWEET_FB_VSYNC_US);
}''', '''	if (rec != NULL) {
		uint64_t begin = mach_absolute_time();
		fb->present(rec);
		uint64_t elapsed = mach_absolute_time() - begin;
		fb->fCopyTicks += elapsed;
		if (elapsed > fb->fCopyMax) { fb->fCopyMax = elapsed; }
		++fb->fPresented;
	}
	fb->finishTick(sender);
}

void
AppleCLCD::finishTick(IOTimerEventSource *sender)
{
	uint64_t now = mach_absolute_time();
	if (fPerfStats && now - fStatsAt >= fStatsPeriod) {
		uint64_t ns, copy_ns, max_ns;
		absolutetime_to_nanoseconds(now - fStatsAt, &ns);
		absolutetime_to_nanoseconds(fCopyTicks, &copy_ns);
		absolutetime_to_nanoseconds(fCopyMax, &max_ns);
		IOLockLock(fLock);
		uint64_t submitted = fSubmitted, overwritten = fOverwritten;
		fSubmitted = fOverwritten = 0;
		bool off = fPowerOff;
		IOLockUnlock(fLock);
		IOLog("SWEET PERF: ms=%llu submitted=%llu presented=%llu overwritten=%llu copy_us=%llu max_us=%llu bytes=%llu missed=%llu off=%u\\n",
		    ns / 1000000, submitted, fPresented, overwritten, copy_ns / 1000,
		    max_ns / 1000, fCopiedBytes, fMissed, off ? 1u : 0u);
		fPresented = fCopyTicks = fCopyMax = fCopiedBytes = fMissed = 0;
		fStatsAt = now;
	}
	if (!fFixedTimer) {
		sender->setTimeoutUS(SWEET_FB_VSYNC_US);
		return;
	}
	// Absolute cadence: skip missed slots, never run a catch-up busy loop.
	now = mach_absolute_time();
	fDeadline += fPeriod;
	if (fDeadline <= now) {
		uint64_t skipped = (now - fDeadline) / fPeriod + 1;
		fDeadline += skipped * fPeriod;
		fMissed += skipped;
	}
	sender->wakeAtTime(fDeadline);
}''')
        text = replace(text, '\tmemcpy(fPending, rec, SWEET_FB_SWAP_REC_SIZE);',
                       '\t++fSubmitted;\n\tif (fDirty) { ++fOverwritten; }\n'
                       '\tmemcpy(fPending, rec, SWEET_FB_SWAP_REC_SIZE);')
        text = replace(text, '\t\tif (src != 0 && bpe == 4) {',
                       '\t\tif (src != 0 && bpe == 4) {\n'
                       '\t\t\tfCopiedBytes += (uint64_t)w * (uint64_t)h * 4;')
    return text


def main():
    patches, output = map(Path, sys.argv[1:])
    if output.exists():
        sys.exit('refusing to overwrite output')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        for patch in sorted(patches.glob('*.patch')):
            subprocess.run(['git', '-C', str(root), 'apply', '--whitespace=nowarn',
                            *[f'--include={p}' for p in PATHS], str(patch.resolve())], check=True)
        result = []
        for path in PATHS:
            before = (root / path).read_text()
            after = transform(path, before)
            result.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                              fromfile='a/' + path, tofile='b/' + path))
        output.write_text(''.join(result))
        subprocess.run(['git', '-C', str(root), 'apply', '--check', str(output.resolve())], check=True)
    print('generated and apply-checked', output)


if __name__ == '__main__':
    main()
