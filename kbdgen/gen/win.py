import io
import os
import os.path
import ntpath
import icu
import unicodedata
import shutil
import concurrent.futures
import uuid

from pathlib import Path
from distutils.dir_util import copy_tree
from textwrap import dedent

from .. import get_logger
from ..filecache import FileCache
from .base import *
from ..cldr import decode_u

logger = get_logger(__file__)

KBDGEN_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "divvun.no")

def guid(kbd_id):
    return uuid.uuid5(KBDGEN_NAMESPACE, kbd_id)

# SC 53 is decimal, 39 is space
WIN_VK_MAP = bind_iso_keys((
    "OEM_5", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "OEM_PLUS", "OEM_4",
    "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "OEM_6", "OEM_1",
    "A", "S", "D", "F", "G", "H", "J", "K", "L", "OEM_3", "OEM_7", "OEM_2",
    "OEM_102", "Z", "X", "C", "V", "B", "N", "M", "OEM_COMMA", "OEM_PERIOD", "OEM_MINUS"
))

WIN_KEYMAP = bind_iso_keys((
    "29", "02", "03", "04", "05", "06", "07", "08", "09", "0a",
    "0b", "0c", "0d", "10", "11", "12", "13", "14", "15", "16",
    "17", "18", "19", "1a", "1b", "1e", "1f", "20", "21", "22",
    "23", "24", "25", "26", "27", "28", "2b", "56", "2c", "2d",
    "2e", "2f", "30", "31", "32", "33", "34", "35"))

DEFAULT_KEYNAMES = """\
KEYNAME

01	Esc
0e	Backspace
0f	Tab
1c	Enter
1d	Ctrl
2a	Shift
36	"Right Shift"
37	"Num *"
38	Alt
39	Space
3a	"Caps Lock"
3b	F1
3c	F2
3d	F3
3e	F4
3f	F5
40	F6
41	F7
42	F8
43	F9
44	F10
45	Pause
46	"Scroll Lock"
47	"Num 7"
48	"Num 8"
49	"Num 9"
4a	"Num -"
4b	"Num 4"
4c	"Num 5"
4d	"Num 6"
4e	"Num +"
4f	"Num 1"
50	"Num 2"
51	"Num 3"
52	"Num 0"
53	"Num Del"
54	"Sys Req"
57	F11
58	F12
7c	F13
7d	F14
7e	F15
7f	F16
80	F17
81	F18
82	F19
83	F20
84	F21
85	F22
86	F23
87	F24

KEYNAME_EXT

1c	"Num Enter"
1d	"Right Ctrl"
35	"Num /"
37	"Prnt Scrn"
38	"Right Alt"
45	"Num Lock"
46	Break
47	Home
48	Up
49	"Page Up"
4b	Left
4d	Right
4f	End
50	Down
51	"Page Down"
52	Insert
53	Delete
54	<00>
56	Help
5b	"Left Windows"
5c	"Right Windows"
5d	Application

"""

def win_filter(*args, force=False):
    def wf(v):
        """actual filter function"""
        if v is None:
            return '-1'

        v = str(v)
        if re.match(r"^\d{4}$", v):
            return v

        v = decode_u(v)

        if v == '\0':
            return '-1'

        # check for anything outsize A-Za-z range
        if not force and re.match("^[A-Za-z]$", v):
            return v

        return "%04x" % ord(v)

    return tuple(wf(i) for i in args)

# Grapheme clusters are known as 'ligatures' in Microsoft jargon.
# This naming is terrible so we're going to use glyphbomb instead.
def win_glyphbomb(v):
    o = tuple('%04x' % ord(c) for c in decode_u(v))
    if len(o) > 4:
        raise Exception('Glyphbombs ("grapheme clusters") cannot be longer than 4 codepoints.')
    return o

inno_langs = {
    "en": "English",
    "nb": "Norwegian"
}

str_enable = {
    "en": "Enable {}",
    "nb": "Aktiver {}"
}

class WindowsGenerator(Generator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cache = FileCache()

    def get_or_download_kbdi(self):
        if os.environ.get("KBDI", None) is not None:
            kbdi = os.environ["KBDI"]
            logger.info("Using kbdi provided by KBDI environment variable: '%s'" % kbdi)
            return kbdi
        kbdi_sha256 = "440209182f9c6015b53616578dba77b83cc1f4a2969d127d8da5d609d297157c"
        kbdi_url = "https://github.com/bbqsrc/kbdi/releases/download/v0.2.0/kbdi.exe"
        return self.cache.download(kbdi_url, kbdi_sha256)

    def generate(self, base='.'):
        outputs = OrderedDict()

        if not self.sanity_check():
            return

        for layout in self.supported_layouts.values():
            outputs[self._klc_get_name(layout, False)] = self.generate_klc(layout)

        if self.dry_run:
            logger.info("Dry run completed.")
            return

        build_dir = os.path.abspath(base)
        os.makedirs(build_dir, exist_ok=True)

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        futures = []

        for name, data in outputs.items():
            klc_path = os.path.join(build_dir, "%s.klc" % name)
            self.write_klc_file(klc_path, data)

            if self.is_release:
                futures.append(executor.submit(self.build_dll, name, "i386", klc_path, build_dir))
                futures.append(executor.submit(self.build_dll, name, "amd64", klc_path, build_dir))
                futures.append(executor.submit(self.build_dll, name, "wow64", klc_path, build_dir))
        
        for future in futures:
            future.result()

        executor.shutdown()

        if self.is_release:
            try:
                kbdi = self.get_or_download_kbdi()
            except Exception as e:
                logger.critical(e)
                return
            shutil.copyfile(kbdi, os.path.join(build_dir, "kbdi.exe"))
            self.generate_inno_script(build_dir)
            self.copy_nlp_files(build_dir)
            self.build_installer(build_dir)
    
    def copy_nlp_files(self, build_dir):
        target = self._project.target('win')
        src_path = target.get("customLocales", None)
        if src_path is None:
            return

        src_path = self._project.relpath(src_path)

        nlp_path = os.path.join(build_dir, "nlp")
        copy_tree(src_path, nlp_path)

    def write_klc_file(self, filepath, data):
        logger.info("Writing '%s'…" % filepath)
        with open(filepath, 'w', encoding='utf-16-le', newline='\r\n') as f:
            f.write('\ufeff')
            f.write(data)

    def sanity_check(self):
        if super().sanity_check() is False:
            return False
        
        for layout in self.supported_layouts.values():
            native_locale = icu.Locale(layout.locale)
            if native_locale.getLCID() == 0 and layout.target("win").get("locale", None) is None:
                logger.error(dedent("""\
                Layout '%s' specifies a locale not recognised by Windows.
                To solve this issue, insert the below into the relevant layout file with the ISO 639-3 code plus the written script of the language in BCP 47 format:

                targets:
                  win:
                    locale: xyz-Latn
                """) % layout.internal_name)
                return False
        
        fail = False
        ids = []
        for layout in self.supported_layouts.values():
            id_ = self._klc_get_name(layout)
            if id_ in ids:
                fail = True
                logger.error("Duplicate id found for '%s': '%s'; set targets.win.id to override.", layout.internal_name, id_)
            else:
                ids.append(id_)

        if fail:
            return False

        if not self.is_release:
            return True

        # Check for wine
        if not shutil.which("wine"):
            logger.error("`wine` must exist on your PATH to build keyboard DLLs.")
            return False

        # Check wine version
        out, err = subprocess.Popen(["wine", "--version"], stdout=subprocess.PIPE).communicate()
        v_chunks = [int(x) for x in out.decode().split("-").pop().split(".")]
        if v_chunks[0] < 2 or (v_chunks[0] == 2 and v_chunks[1]) < 10:
            logger.warn("Builds are not known to succeed with Wine versions less than 2.10; here be dragons.")

        # Check for INNO_PATH
        if os.environ.get("INNO_PATH", None) is None:
            logger.error("INNO_PATH environment variable must point to the Inno Setup 5 directory.")
            return False
        
        # Check for MSKLC_PATH
        if os.environ.get("MSKLC_PATH", None) is None:
            logger.error("MSKLC_PATH environment variable must point to the MSKLC directory.")
            return False

        return True

    def _wine_path(self, thing):
        return "Z:%s" % ntpath.abspath(thing)

    @property
    def _kbdutool(self):
        return "%s/bin/i386/kbdutool.exe" % os.environ["MSKLC_PATH"]

    def build_dll(self, name, arch, klc_path, build_dir):
        # x86, x64, wow64
        flags = {
            "i386": "-x",
            "amd64": "-m",
            "wow64": "-o" 
        }

        flag = flags[arch]

        out_path = os.path.join(build_dir, arch)
        os.makedirs(out_path, exist_ok=True)

        logger.info("Building '%s' for %s…" % (name, arch))
        cmd = ["wine", self._kbdutool, "-n", flag, "-u", self._wine_path(klc_path)]
        run_process(cmd, cwd=out_path)

    def _generate_inno_languages(self):
        out = []
        target = self._project.target('win')
        
        license_format = target.get("licenseFormat", "txt")
        app_license_path = target.get('licensePath', None)
        license_locales = []
        if app_license_path is not None:
            app_license_path = self._project.relpath(app_license_path)
            license_locales = [os.path.splitext(x)[0] for x in os.listdir(app_license_path) if x.endswith(".%s" % license_format)]
            en_license = self._wine_path(os.path.join(app_license_path, "en.%s" % license_format))

        readme_format = target.get("readmeFormat", "txt")
        app_readme_path = target.get('readmePath', None)
        readme_locales = []
        if app_readme_path is not None:
            app_readme_path = self._project.relpath(app_readme_path)
            readme_locales = [os.path.splitext(x)[0] for x in os.listdir(app_readme_path) if x.endswith(".%s" % readme_format)]
            en_readme = self._wine_path(os.path.join(app_readme_path, "en.%s" % readme_format))

        for locale, attrs in self._project.locales.items():
            if locale not in inno_langs:
                logger.info("'%s' not supported by setup script; skipping." % locale)
                continue

            buf = io.StringIO()
            if locale == "en":
                buf.write('Name: "en"; MessagesFile: "compiler:Default.isl"')
            else:
                buf.write('Name: "%s"; MessagesFile: "compiler:Languages\\%s.isl"' % (
                    locale, inno_langs[locale]
                ))
            
            if locale in license_locales:
                p = self._wine_path(os.path.join(app_license_path, "%s.%s" % (locale, license_format)))
                buf.write('; LicenseFile: "%s"' % p)
            elif app_license_path is not None:
                buf.write('; LicenseFile: "%s"' % en_license)
            
            if locale in readme_locales:
                p = self._wine_path(os.path.join(app_readme_path, "%s.%s" % (locale, readme_format)))
                buf.write('; InfoBeforeFile: "%s"' % p)
            elif app_readme_path is not None:
                buf.write('; InfoBeforeFile: "%s"' % en_readme)

            out.append(buf.getvalue())

        return "\n".join(out)

    def _generate_inno_custom_messages(self):
        """Writes out the localised name for the installer and Start Menu group"""
        buf = io.StringIO()
        first_locale = list(self._project.locales.keys())[0]
        for key in inno_langs.keys():
            name = self._project.locales.get(key, first_locale)["name"]
            buf.write("%s.AppName=%s\n" % (key, name))
        return buf.getvalue()

    def generate_inno_script(self, build_dir):
        logger.info("Generating Inno Setup script…")
        target = self._project.target('win')
        try:
            app_version = target['version']
            app_publisher = self._project.organisation
            app_url = target['url']
            app_uuid = target['uuid']
        except KeyError as e:
            logger.error("%s not defined for target" % e)
            sys.exit(1)

        app_license_path = target.get('licensePath', None)
        if app_license_path is not None:
             app_license_path = self._project.relpath(app_license_path)

        app_readme_path = target.get('readmePath', None)
        if app_readme_path is not None:
             app_readme_path = self._project.relpath(app_readme_path)

        script = """\
#define MyAppVersion "%s"
#define MyAppPublisher "%s"
#define MyAppURL "%s"
#define MyAppUUID "%s"
#define BuildDir "%s"

[Setup]
AppId={#MyAppUUID}
AppName={cm:AppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={pf}\\{cm:AppName}
DisableDirPage=no
DefaultGroupName={cm:AppName}
OutputBaseFilename=install
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
AlwaysRestart=yes
AllowCancelDuringInstall=no
UninstallRestartComputer=yes
UninstallDisplayName={cm:AppName}

[Languages]
%s

[CustomMessages]
en.Enable=Enable %%1
nb.Enable=Aktiver %%1
%s

[Files]
Source: "{#BuildDir}\\kbdi.exe"; DestDir: "{app}"
Source: "{#BuildDir}\\i386\\*"; DestDir: "{sys}"; Check: not Is64BitInstallMode; Flags: restartreplace uninsrestartdelete
Source: "{#BuildDir}\\amd64\\*"; DestDir: "{sys}"; Check: Is64BitInstallMode; Flags: restartreplace uninsrestartdelete
Source: "{#BuildDir}\\wow64\\*"; DestDir: "{syswow64}"; Check: Is64BitInstallMode; Flags: restartreplace uninsrestartdelete
        """.strip() % (
            app_version,
            app_publisher,
            app_url,
            app_uuid,
            self._wine_path(build_dir),
            self._generate_inno_languages(),
            self._generate_inno_custom_messages()
        )
        
        custom_locales = target.get("customLocales", None)

        if custom_locales is not None:
            custom_locales_path = self._project.relpath(custom_locales)
            locales = [os.path.splitext(x)[0] for x in os.listdir(custom_locales_path) if x.endswith(".nlp")]
            reg = []

            for l in locales:
                o = dedent("""
                Root: HKLM; Subkey: "SYSTEM\\CurrentControlSet\\Control\\Nls\\CustomLocale";
                ValueType: string; ValueName: "{locale}"; ValueData: "{locale}"; Flags: uninsdeletevalue
                """).strip().replace("\n", " ").format(locale=l)
                reg.append(o)

            script += """Source: "{#BuildDir}\\nlp\\*"; DestDir: "{win}\\Globalization"; Flags: restartreplace uninsrestartdelete\n"""
            script += "\n[Registry]\n"
            script += "\n".join(reg)
            script += "\n"      

        # Add Run section
        run_scr = io.StringIO()
        run_scr.write("[Run]\n")
        uninst_scr = io.StringIO()
        uninst_scr.write("[UninstallRun]\n")
        icons_scr = io.StringIO()
        icons_scr.write("[Icons]\n")

        for layout in self.supported_layouts.values():
            kbd_id = self._klc_get_name(layout)
            dll_name = "%s.dll" % kbd_id
            language_code = layout.target("win").get("locale", layout.locale)
            language_name = layout.target("win").get("languageName", None)
            if language_name is not None:
                logger.info("Using language name '%s' for layout '%s'." % (language_name, layout.internal_name))
            else:
                logger.info("Using Windows default language name for layout '%s'; this can be overridden by providing a value for targets.win.languageName." % (language_name, layout.internal_name))
            guid_str = "{%s}" % str(guid(kbd_id))

            # Pre-install clean script
            run_scr.write('Filename: "{app}\\kbdi.exe"; Parameters: "clean"; '
                          'Flags: runhidden waituntilterminated\n')

            # Install script
            run_scr.write('Filename: "{app}\\kbdi.exe"; Parameters: "keyboard_install')
            run_scr.write(' -t ""%s""' % language_code) # BCP 47 tag
            if language_name:
                run_scr.write(' -l ""%s""' % language_name) # Language display name
            run_scr.write(' -g ""{%s""' % guid_str) # Product code
            run_scr.write(" -d %s" % dll_name) # Layout DLL
            run_scr.write(' -n ""%s""' % layout.native_display_name) # Layout native display name
            run_scr.write(' -e') # Enable layout after installing it
            run_scr.write('"; Flags: runhidden waituntilterminated\n')

            # Enablement icon
            icons_scr.write('Name: "{group}\\{cm:Enable,%s}"; ' % layout.native_display_name)
            icons_scr.write('Filename: "{app}\\kbdi.exe"; ')
            icons_scr.write('Parameters: "keyboard_enable -g ""{%s"" -t %s"; ' % (guid_str, language_code))
            icons_scr.write('Flags: runminimized preventpinning excludefromshowinnewinstall\n')

            # Uninstall script
            uninst_scr.write('Filename: "{app}\\kbdi.exe"; Parameters: "keyboard_uninstall')
            uninst_scr.write(' ""{%s"""; Flags: runhidden waituntilterminated\n' % guid_str)      

        script = "\n\n".join((
            script,
            run_scr.getvalue(),
            uninst_scr.getvalue(),
            icons_scr.getvalue()
        ))

        with open(os.path.join(build_dir, "install.iss"), 'w', encoding='utf-8-sig', newline='\r\n') as f:
            f.write(script)

    def build_installer(self, build_dir):
        logger.info("Building installer…")
        iscc = os.path.join(os.environ["INNO_PATH"], "ISCC.exe")
        output_path = self._wine_path(build_dir)
        script_path = self._wine_path(os.path.join(build_dir, "install.iss"))
        
        first_locale = list(self._project.locales.keys())[0]
        name = self._project.locales[first_locale]["name"]
        version = self._project.target('win')['version']

        cmd = ["wine", iscc, '/O%s' % output_path, script_path]
        run_process(cmd, cwd=build_dir)

        fn = "%s_%s.exe" % (name.replace(" ", "_"), version)
        shutil.move(os.path.join(build_dir, "install.exe"), os.path.join(build_dir, fn))

        logger.info("Installer generated at '%s'." % 
            os.path.join(build_dir, fn))

    def _klc_get_name(self, layout, show_errors=True):
        id_ = layout.target('win').get("id", None)
        if id_ is not None:
            if len(id_) != 5 and show_errors:
                logger.warning("Keyboard id '%s' should be exactly 5 characters, got %d." % (id_, len(id_)))
            return "kbd" + id_
        return "kbd" + re.sub(r'[^A-Za-z0-9-]', "", layout.internal_name)[:5]

    def _klc_write_headers(self, layout, buf):
        buf.write('KBD\t%s\t"%s"\n\n' % (
            self._klc_get_name(layout, False),
            layout.display_names[layout.locale]))

        copyright_ = self._project.copyright
        organisation = self._project.organisation
        locale = layout.target('win').get("locale", layout.locale)

        buf.write('COPYRIGHT\t"%s"\n\n' % copyright_)
        buf.write('COMPANY\t"%s"\n\n' % organisation)
        buf.write('LOCALENAME\t"%s"\n\n' % layout.locale)

        lcid = icu.Locale(locale).getLCID()
        if lcid != 0:
            locale_id = "0000%04x" % lcid
        else:
            locale_id = "00001000"

        # Use fallback ID in every case (MS-LCID)
        buf.write('LOCALEID\t"%s"\n\n' % locale_id)
        buf.write('VERSION\t1.0\n\n')
        # 0: default, 1: shift, 2: ctrl, 6: altGr/ctrl+alt, 7: shift+6
        buf.write('SHIFTSTATE\n\n0\n1\n2\n6\n7\n\n')

        buf.write('LAYOUT       ;\n\n')
        buf.write('//SC\tVK_ \t\tCaps\tNormal\tShift\tCtrl\tAltGr\tAltShft\t-> Output\n')
        buf.write('//--\t----\t\t----\t------\t-----\t----\t-----\t-------\t   ------\n\n')

    def _klc_write_keys(self, layout, buf):
        col0 = mode_iter(layout, 'iso-default', required=True)
        col1 = mode_iter(layout, 'iso-shift')
        col2 = mode_iter(layout, 'iso-ctrl')
        col6 = mode_iter(layout, 'iso-alt')
        col7 = mode_iter(layout, 'iso-alt+shift')
        alt_caps = mode_iter(layout, 'iso-alt+caps')
        caps = mode_iter(layout, 'iso-caps')
        caps_shift = mode_iter(layout, 'iso-caps+shift')

        # Hold all the glyphbombs
        glyphbombs = []

        for (sc, vk, c0, c1, c2, c6, c7, cap, scap, acap) in zip(
                WIN_KEYMAP.values(), WIN_VK_MAP.values(), col0, col1, col2,
                col6, col7, caps, caps_shift, alt_caps):

            cap_mode = 0
            if cap is not None and c0 != cap and c1 != cap:
                cap_mode = "SGCap"
            elif cap is None:
                cap_mode += 1 if c0 != c1 else 0
                cap_mode += 4 if c6 != c7 else 0
            else:
                cap_mode += 1 if cap == c1 else 0
                cap_mode += 4 if acap == c7 else 0
            cap_mode = str(cap_mode)

            if len(vk) < 8:
                vk += "\t"
            buf.write("%s\t%s\t%s" % (sc, vk, cap_mode))

            # n is the col number for glyphbombs.
            for n, mode, key in ((0, 'iso-default', c0),
                                 (1, 'iso-shift', c1),
                                 (2, 'iso-ctrl', c2),
                                 (3, 'iso-alt', c6),
                                 (4, 'iso-alt+shift', c7)):

                filtered = decode_u(key or '')
                if key is not None and len(filtered) > 1:
                    buf.write("\t%%")
                    glyphbombs.append((filtered, (vk, str(n)) + win_glyphbomb(key)))
                else:
                    buf.write("\t%s" % win_filter(key))
                    if key in layout.dead_keys.get(mode, []):
                        buf.write("@")

            buf.write("\t// %s %s %s %s %s\n" % (c0, c1, c2, c6, c7))

            if cap_mode == "SGCap":
                if cap is not None and len(win_glyphbomb(cap)) > 1:
                    cap = None
                    logger.error("Caps key '%s' is a glyphbomb and cannot be used in Caps Mode." % cap)

                if scap is not None and len(win_glyphbomb(scap)) > 1:
                    scap = None
                    logger.error("Caps+Shift key '%s' is a glyphbomb and cannot be used in Caps Mode." % cap)

                buf.write("-1\t-1\t\t0\t%s\t%s\t\t\t\t// %s %s\n" % (
                    win_filter(cap, scap) + (cap, scap)))

        # Space, such special case oh my.
        buf.write("39\tSPACE\t\t0\t")
        if 'space' not in layout.special:
            buf.write("0020\t0020\t0020\t-1\t-1\n")
        else:
            o = layout.special['space']
            buf.write("%s\t%s\t%s\t%s\t%s\n" % win_filter(
                    o.get('iso-default', '0020'),
                    o.get('iso-shift', '0020'),
                    o.get('iso-ctrl', '0020'),
                    o.get('iso-alt', None),
                    o.get('iso-alt+shift', None)
                ))

        # Decimal key on keypad.
        decimal = layout.decimal or "."
        buf.write("53\tDECIMAL\t\t0\t%s\t%s\t-1\t-1\t-1\n\n" % win_filter(
            decimal, decimal))

        # Glyphbombs!
        if len(glyphbombs) > 0:
            buf.write("LIGATURE\n\n")
            buf.write("//VK_\tMod#\tChr0\tChr1\tChr2\tChr3\n")
            buf.write("//----\t----\t----\t----\t----\t----\n\n")
            for original, col in glyphbombs:
                more_tabs = len(col)-7
                buf.write("%s\t\t%s%s\t// %s\n" % (col[0],
                    "\t".join(col[1:]),
                    '\t' * more_tabs,
                    original))
            buf.write('\n')

        # Deadkeys!
        for basekey, o in layout.transforms.items():
            if len(basekey) != 1:
                logger.warning(("Base key '%s' invalid for Windows " +
                       "deadkeys; skipping.") % basekey)
                continue

            buf.write("DEADKEY\t%s\n\n" % win_filter(basekey))
            for key, output in o.items():
                if key == ' ':
                    continue

                key = str(key)
                output = str(output)

                if len(key) != 1 or len(output) != 1:
                    logger.warning(("%s%s -> %s is invalid for Windows " +
                           "deadkeys; skipping.") % (basekey, key, output))
                    continue
                buf.write("%s\t%s\t// %s -> %s\n" % (
                    win_filter(key, output, force=True) + (key, output)))

            # Create fallback key from space, or the basekey.
            output = o.get(' ', basekey)
            buf.write("0020\t%s\t//   -> %s\n\n" % (
                win_filter(output)[0], output))
    
    def _klc_write_deadkey_names(self, layout, buf):
        buf.write("KEYNAME_DEAD\n\n")

        for basekey, o in layout.transforms.items():
            if len(basekey) != 1:
                logger.warning(("Base key '%s' invalid for Windows " +
                       "deadkeys; skipping.") % basekey)
                continue
            
            buf.write('%s\t"%s"\n' % (win_filter(basekey)[0], unicodedata.name(basekey)))

    def _klc_write_footer(self, layout, buf):
        out = []

        # Check native name
        native_locale = icu.Locale(layout.locale)

        out.append((0x0c00, native_locale.getDisplayName(), layout.native_display_name))
        out.append((0x1000, native_locale.getDisplayName(), layout.native_display_name))

        if native_locale.getLCID() != 0:
            out.append((native_locale.getLCID(), native_locale.getDisplayName(), layout.native_display_name))

        for locale_code, name in layout.display_names.items():
            if locale_code == layout.locale:
                continue

            locale = icu.Locale(locale_code)
            language_name = locale.getDisplayName()
            lcid = locale.getLCID()

            if language_name == locale_code or lcid == 0:
                continue

            out.append((lcid, language_name, name))

        buf.write("\nDESCRIPTIONS\n\n")
        for item in out:
            buf.write("%04x\t%s\n" % (item[0], item[2]))

        buf.write("\nLANGUAGENAMES\n\n")
        for item in out:
            buf.write("%04x\t%s\n" % (item[0], item[1]))
        
        buf.write("ENDKBD\n")

    def generate_klc(self, layout):
        buf = io.StringIO()

        self._klc_write_headers(layout, buf)
        self._klc_write_keys(layout, buf)
        buf.write(DEFAULT_KEYNAMES)
        self._klc_write_deadkey_names(layout, buf)
        self._klc_write_footer(layout, buf)

        return buf.getvalue()

