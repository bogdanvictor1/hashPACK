import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import hashlib
import zipfile
import tarfile
import os
import time
import json
from pathlib import Path
from datetime import datetime

# ── Dependências opcionais ────────────────────────────────────────────────────
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

try:
    import py7zr
    HAS_7Z = True
except ImportError:
    HAS_7Z = False


# ── Paleta ────────────────────────────────────────────────────────────────────
DARK_BG      = "#0f1117"
PANEL_BG     = "#1a1d27"
CARD_BG      = "#222535"
DROP_HOVER   = "#1e2540"
ACCENT       = "#5b6af0"
ACCENT_LIGHT = "#7b8aff"
SUCCESS      = "#22c55e"
WARNING      = "#f59e0b"
TEXT_PRIMARY = "#e8eaf6"
TEXT_SEC     = "#8b92b8"
BORDER       = "#2e3352"
DISABLED_FG  = "#444966"

ALGORITHMS = ["MD5", "SHA-1", "SHA-256", "SHA-512"]
HASH_MAP   = {"MD5": "md5", "SHA-1": "sha1", "SHA-256": "sha256", "SHA-512": "sha512"}

FORMATS_LEVELS = {
    "ZIP":     {"levels": ["Sem compactação (0)", "Rápido (1)", "Normal (6)", "Máximo (9)"],
                "values": [0, 1, 6, 9], "default": 2},
    "TAR.GZ":  {"levels": ["Rápido (1)", "Normal (6)", "Máximo (9)"],
                "values": [1, 6, 9],   "default": 1},
    "TAR.BZ2": {"levels": ["Rápido (1)", "Normal (6)", "Máximo (9)"],
                "values": [1, 6, 9],   "default": 1},
    "7Z":      {"levels": ["Rápido (1)", "Normal (5)", "Máximo (9)"],
                "values": [1, 5, 9],   "default": 1},
}


# ── Lógica de hash ────────────────────────────────────────────────────────────
def calc_hash(path: str, algo: str, progress_cb=None) -> str:
    h    = hashlib.new(HASH_MAP[algo])
    size = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
            done += len(chunk)
            if progress_cb and size:
                progress_cb(done / size * 100)
    return h.hexdigest()


def calc_all_hashes(srcs: list, algos: list, status_cb=None) -> dict:
    """
    Calcula hashes de todos os arquivos em srcs (lista de caminhos).
    Retorna estrutura organizada por arquivo:
      {
        "arquivo.txt":          { "MD5": "…", "SHA-256": "…" },
        "pasta/subarq.bin":     { "MD5": "…", "SHA-256": "…" },
        "__combined__":         { "MD5": "…", "SHA-256": "…" }  ← se > 1 arquivo
      }
    """
    result    = {}
    combiners = {algo: hashlib.new(HASH_MAP[algo]) for algo in algos}

    for src in srcs:
        if os.path.isfile(src):
            name = os.path.basename(src)
            result[name] = {}
            for algo in algos:
                if status_cb: status_cb(f"Hash {algo} — {name}…")
                h = calc_hash(src, algo)
                result[name][algo] = h
                combiners[algo].update(h.encode())
        else:
            # Diretório: percorre recursivamente
            all_files = sorted(
                os.path.join(root, fn)
                for root, _, fns in os.walk(src)
                for fn in fns
            )
            base = os.path.dirname(src)
            for fp in all_files:
                rel = os.path.relpath(fp, base)
                result[rel] = {}
                for algo in algos:
                    if status_cb: status_cb(f"Hash {algo} — {os.path.basename(fp)}…")
                    h = calc_hash(fp, algo)
                    result[rel][algo] = h
                    combiners[algo].update(h.encode())

    # Hash combinado (útil quando há mais de um arquivo/diretório)
    if len(result) > 1:
        result["__combined__"] = {algo: combiners[algo].hexdigest() for algo in algos}

    return result


def dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def total_src_size(srcs: list) -> int:
    total = 0
    for src in srcs:
        total += os.path.getsize(src) if os.path.isfile(src) else dir_size(src)
    return total


# ── Lógica de compactação ─────────────────────────────────────────────────────
def _collect_entries(srcs: list, dest_abs: str) -> list:
    """
    Retorna lista de (filepath_abs, arcname) para todos os arquivos em srcs,
    excluindo o próprio arquivo de destino (evita loop infinito).
    """
    entries = []
    for src in srcs:
        if os.path.isfile(src):
            if os.path.abspath(src) != dest_abs:
                entries.append((src, os.path.basename(src)))
        else:
            base = os.path.dirname(src)
            for root, _, fns in os.walk(src):
                for fn in fns:
                    fp = os.path.join(root, fn)
                    if os.path.abspath(fp) == dest_abs:
                        continue   # ← nunca incluir o arquivo que está sendo criado
                    arcname = os.path.relpath(fp, base)
                    entries.append((fp, arcname))
    return entries


def compress(srcs: list, dest: str, fmt: str, level: int, progress_cb=None) -> None:
    dest_abs = os.path.abspath(dest)

    if fmt == "ZIP":
        comp = zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED
        kw   = {} if level == 0 else {"compresslevel": level}
        # Coleta ANTES de abrir o arquivo para não incluí-lo na varredura
        entries = _collect_entries(srcs, dest_abs)
        with zipfile.ZipFile(dest, "w", comp, **kw) as zf:
            for i, (fp, arcname) in enumerate(entries, 1):
                zf.write(fp, arcname)
                if progress_cb: progress_cb(i / max(len(entries), 1) * 100)

    elif fmt in ("TAR.GZ", "TAR.BZ2"):
        mode    = "w:gz" if fmt == "TAR.GZ" else "w:bz2"
        entries = _collect_entries(srcs, dest_abs)
        try:
            tf_open = lambda: tarfile.open(dest, mode, compresslevel=level)
            tf_open()  # test if compresslevel is supported
        except TypeError:
            tf_open = lambda: tarfile.open(dest, mode)
        with tf_open() as tf:
            for fp, arcname in entries:
                tf.add(fp, arcname=arcname)
        if progress_cb: progress_cb(100)

    elif fmt == "7Z":
        if not HAS_7Z:
            raise RuntimeError("py7zr não instalado. Execute: pip install py7zr")
        entries = _collect_entries(srcs, dest_abs)
        filters = [{"id": py7zr.FILTER_LZMA2, "preset": level}]
        with py7zr.SevenZipFile(dest, "w", filters=filters) as sz:
            for fp, arcname in entries:
                sz.write(fp, arcname)
        if progress_cb: progress_cb(100)


# ── Parsing de caminhos drag & drop ───────────────────────────────────────────
def parse_dnd_paths(raw: str) -> list:
    """
    tkinterdnd2 retorna caminhos separados por espaço; caminhos com espaços
    ficam entre chaves: {/path/with spaces/file} /simple/path
    """
    paths = []
    raw   = raw.strip()
    i     = 0
    while i < len(raw):
        if raw[i] == "{":
            end = raw.find("}", i)
            if end == -1:
                paths.append(raw[i+1:])
                break
            paths.append(raw[i+1:end])
            i = end + 1
        elif raw[i] == " ":
            i += 1
        else:
            end = raw.find(" ", i)
            if end == -1:
                paths.append(raw[i:])
                break
            paths.append(raw[i:end])
            i = end + 1
    return [os.path.normpath(p) for p in paths if p.strip()]


# ── Widget: zona de drag & drop / seleção de origem ──────────────────────────
class DropZone(tk.Frame):
    def __init__(self, parent, on_paths_changed, on_browse_files, on_browse_dir, **kw):
        super().__init__(parent, bg=DARK_BG, **kw)
        self._on_changed  = on_paths_changed
        self._paths: list = []

        # Área de soltar
        self._canvas = tk.Canvas(
            self, bg=CARD_BG, highlightthickness=1,
            highlightbackground=BORDER, height=90, cursor="hand2"
        )
        self._canvas.pack(fill="x", pady=(0, 8))
        self._canvas.bind("<Configure>", self._redraw)
        if HAS_DND:
            self._canvas.drop_target_register(DND_FILES)
            self._canvas.dnd_bind("<<Drop>>",     self._handle_drop)
            self._canvas.dnd_bind("<<DragEnter>>", lambda e: self._set_hover(True))
            self._canvas.dnd_bind("<<DragLeave>>", lambda e: self._set_hover(False))
        self._canvas.bind("<Button-1>", lambda e: on_browse_files())
        self._hover = False

        # Linha campo + botões
        row = tk.Frame(self, bg=DARK_BG)
        row.pack(fill="x")
        tk.Label(row, text="SELECIONADO:", bg=DARK_BG, fg=TEXT_SEC,
                 font=("Courier New", 8, "bold")).pack(side="left", padx=(0, 8))
        self._var   = tk.StringVar()
        self._entry = tk.Entry(row, textvariable=self._var, bg=CARD_BG,
                               fg=TEXT_PRIMARY, insertbackground=ACCENT_LIGHT,
                               relief="flat", font=("Courier New", 9), bd=0,
                               state="readonly")
        self._entry.pack(side="left", fill="x", expand=True, ipady=6, ipadx=8)
        for label, cmd in [("arquivos", on_browse_files), ("pasta", on_browse_dir)]:
            tk.Button(row, text=label, command=cmd,
                      bg=ACCENT, fg="white", relief="flat",
                      font=("Courier New", 8, "bold"), cursor="hand2",
                      activebackground=ACCENT_LIGHT, activeforeground="white",
                      padx=10, pady=4).pack(side="left", padx=(5, 0))
        tk.Button(row, text="✕", command=self.clear,
                  bg=CARD_BG, fg=TEXT_SEC, relief="flat",
                  font=("Courier New", 8), cursor="hand2",
                  padx=6, pady=4).pack(side="left", padx=(5, 0))

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", pady=(8, 0))
        self.after(100, self._redraw)

    # ── Desenho ───────────────────────────────────────────────────────────────
    def _redraw(self, *_):
        c   = self._canvas
        w   = c.winfo_width()  or 400
        h   = c.winfo_height() or 90
        c.delete("all")
        c.configure(bg=DROP_HOVER if self._hover else CARD_BG,
                    highlightbackground=ACCENT if self._hover else BORDER)

        n = len(self._paths)
        if n == 0:
            icon  = "⤵" if HAS_DND else "⬡"
            line1 = "Arraste arquivos ou pasta aqui" if HAS_DND \
                    else "Use os botões para selecionar"
            line2 = "ou use os botões abaixo" if HAS_DND \
                    else "instale tkinterdnd2 para drag & drop"
            c1, c2 = TEXT_SEC, TEXT_SEC
        elif n == 1:
            icon  = "📄" if os.path.isfile(self._paths[0]) else "📁"
            name  = os.path.basename(self._paths[0])
            line1 = name[:62] + ("…" if len(name) > 62 else "")
            line2 = "clique ou solte para substituir"
            c1, c2 = TEXT_PRIMARY, TEXT_SEC
        else:
            icon  = "📦"
            line1 = f"{n} itens selecionados"
            names = ", ".join(os.path.basename(p) for p in self._paths[:3])
            if n > 3: names += f" e mais {n-3}…"
            line2 = names[:68]
            c1, c2 = TEXT_PRIMARY, TEXT_SEC

        c.create_text(w//2, h//2 - 16, text=icon,
                      fill=ACCENT_LIGHT if self._hover else TEXT_SEC,
                      font=("Courier New", 18))
        c.create_text(w//2, h//2 + 8,  text=line1,
                      fill=ACCENT_LIGHT if self._hover else c1,
                      font=("Courier New", 9, "bold" if n else "normal"))
        c.create_text(w//2, h//2 + 24, text=line2,
                      fill=TEXT_SEC, font=("Courier New", 8))

    def _set_hover(self, v: bool):
        self._hover = v
        self._redraw()

    # ── D&D ───────────────────────────────────────────────────────────────────
    def _handle_drop(self, e):
        self._set_hover(False)
        paths = parse_dnd_paths(e.data)
        if paths:
            self.set_paths(paths)

    # ── API pública ───────────────────────────────────────────────────────────
    def get_paths(self) -> list:
        return list(self._paths)

    def set_paths(self, paths: list):
        self._paths = [p for p in paths if os.path.exists(p)]
        if not self._paths:
            self._var.set("")
        elif len(self._paths) == 1:
            self._var.set(self._paths[0])
        else:
            self._var.set(f"[{len(self._paths)} itens]  " +
                          "  |  ".join(os.path.basename(p) for p in self._paths[:4]))
        self._redraw()
        self._on_changed(self._paths)

    def clear(self):
        self.set_paths([])


# ── Widget: campo de destino com botão ────────────────────────────────────────
class PathEntry(tk.Frame):
    def __init__(self, parent, label, select_fn, bg=DARK_BG, **kw):
        super().__init__(parent, bg=bg, **kw)
        tk.Label(self, text=label, bg=bg, fg=TEXT_SEC,
                 font=("Courier New", 9, "bold")).pack(anchor="w", pady=(0, 4))
        row = tk.Frame(self, bg=bg)
        row.pack(fill="x")
        self.var   = tk.StringVar()
        self._entry = tk.Entry(row, textvariable=self.var, bg=CARD_BG,
                               fg=TEXT_PRIMARY, insertbackground=ACCENT_LIGHT,
                               relief="flat", font=("Courier New", 10), bd=0)
        self._entry.pack(side="left", fill="x", expand=True, ipady=8, ipadx=10)
        self._btn = tk.Button(row, text="…", command=select_fn,
                              bg=ACCENT, fg="white", relief="flat",
                              font=("Courier New", 11, "bold"), cursor="hand2",
                              activebackground=ACCENT_LIGHT, activeforeground="white",
                              padx=12)
        self._btn.pack(side="left", padx=(6, 0))
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", pady=(4, 0))

    def get(self):  return self.var.get()
    def set(self, v): self.var.set(v)

    def set_state(self, enabled: bool):
        self._entry.config(state="normal" if enabled else "disabled")
        self._btn.config(state="normal" if enabled else "disabled",
                         bg=ACCENT if enabled else CARD_BG,
                         fg="white" if enabled else DISABLED_FG)


# ── Widget: barra de progresso customizada ────────────────────────────────────
class ColorBar(tk.Canvas):
    def __init__(self, parent, **kw):
        super().__init__(parent, height=6, bg=CARD_BG, highlightthickness=0, **kw)
        self._pct = 0
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, pct):
        self._pct = max(0.0, min(100.0, pct))
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        self.create_rectangle(0, 0, int(w * self._pct / 100), 6,
                               fill=SUCCESS if self._pct >= 100 else ACCENT,
                               width=0)


# ── Janela principal ──────────────────────────────────────────────────────────
_Base = TkinterDnD.Tk if HAS_DND else tk.Tk


class App(_Base):
    def __init__(self):
        super().__init__()
        self.title("Compactador + Hash  v1.4")
        self.configure(bg=DARK_BG)
        self.resizable(True, True)
        self.minsize(740, 720)
        self._report = None
        self._build()
        self._center()

    def _center(self):
        self.update_idletasks()
        w, h = 880, 840
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # ── Layout ───────────────────────────────────────────────────────────────
    def _build(self):
        # Cabeçalho
        hdr = tk.Frame(self, bg=PANEL_BG, pady=16)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⬡  COMPACTADOR",
                 bg=PANEL_BG, fg=ACCENT_LIGHT,
                 font=("Courier New", 22, "bold")).pack()
        badges = (("drag & drop ✓" if HAS_DND else "drag & drop ✗ [pip install tkinterdnd2]") +
                  "   ·   " +
                  ("7Z ✓" if HAS_7Z else "7Z ✗ [pip install py7zr]"))
        tk.Label(hdr, text=badges, bg=PANEL_BG, fg=TEXT_SEC,
                 font=("Courier New", 8)).pack()
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # Corpo rolável
        outer  = tk.Frame(self, bg=DARK_BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=DARK_BG, highlightthickness=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body   = tk.Frame(canvas, bg=DARK_BG)
        win_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        P = 28

        # ── 01  ORIGEM ──────────────────────────────────────────────────────
        self._section(body, "01  ORIGEM", P)
        self.drop_zone = DropZone(
            body,
            on_paths_changed = self._on_paths_changed,
            on_browse_files  = self._pick_files,
            on_browse_dir    = self._pick_dir,
        )
        self.drop_zone.pack(fill="x", padx=P, pady=(0, 6))

        cb_row = tk.Frame(body, bg=DARK_BG)
        cb_row.pack(fill="x", padx=P, pady=(4, 12))
        self.use_src_dir = tk.BooleanVar(value=False)
        tk.Checkbutton(
            cb_row,
            text="  Salvar arquivo compactado no diretório de origem",
            variable=self.use_src_dir,
            command=self._toggle_dst,
            bg=DARK_BG, fg=TEXT_PRIMARY, selectcolor=CARD_BG,
            activebackground=DARK_BG, activeforeground=ACCENT_LIGHT,
            font=("Courier New", 9), cursor="hand2"
        ).pack(side="left")

        # ── 02  DESTINO ─────────────────────────────────────────────────────
        self._section(body, "02  DESTINO", P)
        self.dst_entry = PathEntry(body, "Pasta de destino (e do relatório JSON):",
                                   self._pick_dst)
        self.dst_entry.pack(fill="x", padx=P, pady=(0, 14))

        # ── 03  OPÇÕES ──────────────────────────────────────────────────────
        self._section(body, "03  OPÇÕES", P)
        opt_row = tk.Frame(body, bg=DARK_BG)
        opt_row.pack(fill="x", padx=P, pady=(0, 14))

        # Formato
        c1 = tk.Frame(opt_row, bg=DARK_BG)
        c1.pack(side="left", fill="x", expand=True, padx=(0, 12))
        tk.Label(c1, text="FORMATO", bg=DARK_BG, fg=TEXT_SEC,
                 font=("Courier New", 9, "bold")).pack(anchor="w", pady=(0, 4))
        self.fmt_var = tk.StringVar(value="ZIP")
        fmts = list(FORMATS_LEVELS.keys()) if HAS_7Z else \
               [k for k in FORMATS_LEVELS if k != "7Z"]
        self.fmt_combo = ttk.Combobox(c1, textvariable=self.fmt_var,
                                      values=fmts, state="readonly",
                                      font=("Courier New", 10))
        self.fmt_combo.pack(fill="x", ipady=6)
        self.fmt_combo.bind("<<ComboboxSelected>>", self._on_fmt_change)

        # Grau
        c2 = tk.Frame(opt_row, bg=DARK_BG)
        c2.pack(side="left", fill="x", expand=True, padx=(0, 12))
        tk.Label(c2, text="GRAU DE COMPACTAÇÃO", bg=DARK_BG, fg=TEXT_SEC,
                 font=("Courier New", 9, "bold")).pack(anchor="w", pady=(0, 4))
        self.lvl_var = tk.StringVar()
        self.lvl_combo = ttk.Combobox(c2, textvariable=self.lvl_var,
                                      state="readonly", font=("Courier New", 10))
        self.lvl_combo.pack(fill="x", ipady=6)

        # Algoritmos
        c3 = tk.Frame(opt_row, bg=DARK_BG)
        c3.pack(side="left", fill="x", expand=True)
        tk.Label(c3, text="ALGORITMOS DE HASH", bg=DARK_BG, fg=TEXT_SEC,
                 font=("Courier New", 9, "bold")).pack(anchor="w", pady=(0, 4))
        self.algo_vars = {}
        for a in ALGORITHMS:
            v = tk.BooleanVar(value=(a in ("MD5", "SHA-256")))
            self.algo_vars[a] = v
            tk.Checkbutton(c3, text=a, variable=v,
                           bg=DARK_BG, fg=TEXT_PRIMARY, selectcolor=CARD_BG,
                           activebackground=DARK_BG, activeforeground=ACCENT_LIGHT,
                           font=("Courier New", 9), cursor="hand2"
                           ).pack(anchor="w")

        self._on_fmt_change()

        # ── 04  PROGRESSO ───────────────────────────────────────────────────
        self._section(body, "04  PROGRESSO", P)
        prog = tk.Frame(body, bg=CARD_BG, pady=14, padx=14)
        prog.pack(fill="x", padx=P, pady=(0, 14))
        self.status_lbl = tk.Label(prog, text="Aguardando…",
                                   bg=CARD_BG, fg=TEXT_SEC,
                                   font=("Courier New", 9), anchor="w")
        self.status_lbl.pack(fill="x")
        self.bar = ColorBar(prog)
        self.bar.pack(fill="x", pady=(8, 4))

        # Botão iniciar
        self.run_btn = tk.Button(body, text="▶  INICIAR",
                                 command=self._start,
                                 bg=ACCENT, fg="white", relief="flat",
                                 font=("Courier New", 13, "bold"),
                                 cursor="hand2", pady=12,
                                 activebackground=ACCENT_LIGHT,
                                 activeforeground="white")
        self.run_btn.pack(fill="x", padx=P, pady=(0, 14))

        # ── 05  RESULTADOS ──────────────────────────────────────────────────
        self._section(body, "05  RESULTADOS", P)
        res_wrap = tk.Frame(body, bg=CARD_BG)
        res_wrap.pack(fill="both", expand=True, padx=P, pady=(0, 4))
        self.result_text = tk.Text(
            res_wrap, bg=CARD_BG, fg=TEXT_PRIMARY,
            font=("Courier New", 9), relief="flat", wrap="none",
            state="disabled", height=14, padx=12, pady=10,
            selectbackground=ACCENT
        )
        sb_v = ttk.Scrollbar(res_wrap, orient="vertical",
                              command=self.result_text.yview)
        sb_h = ttk.Scrollbar(res_wrap, orient="horizontal",
                              command=self.result_text.xview)
        self.result_text.configure(yscrollcommand=sb_v.set,
                                   xscrollcommand=sb_h.set)
        sb_v.pack(side="right",  fill="y")
        sb_h.pack(side="bottom", fill="x")
        self.result_text.pack(fill="both", expand=True)

        for tag, fg, bold in [
            ("header", ACCENT_LIGHT, True), ("ok",    SUCCESS,      False),
            ("warn",   WARNING,      False), ("label", TEXT_SEC,    False),
            ("value",  TEXT_PRIMARY, False), ("file",  ACCENT,      False),
            ("dim",    DISABLED_FG,  False),
        ]:
            kw = {"foreground": fg}
            if bold: kw["font"] = ("Courier New", 9, "bold")
            self.result_text.tag_config(tag, **kw)

        self.export_btn = tk.Button(
            body, text="💾  EXPORTAR RELATÓRIO JSON NOVAMENTE",
            command=self._export_manual,
            bg=CARD_BG, fg=TEXT_SEC, relief="flat",
            font=("Courier New", 9), cursor="hand2",
            pady=6, activebackground=BORDER, state="disabled"
        )
        self.export_btn.pack(fill="x", padx=P, pady=(4, 20))

    # ── Helpers de layout ────────────────────────────────────────────────────
    def _section(self, parent, title, padx=0):
        f = tk.Frame(parent, bg=DARK_BG)
        f.pack(fill="x", padx=padx, pady=(8, 8))
        tk.Label(f, text=title, bg=DARK_BG, fg=ACCENT,
                 font=("Courier New", 10, "bold")).pack(side="left")
        tk.Frame(f, bg=BORDER, height=1).pack(side="left",
                                               fill="x", expand=True, padx=(10, 0))

    # ── Callbacks de UI ──────────────────────────────────────────────────────
    def _on_fmt_change(self, *_):
        info = FORMATS_LEVELS.get(self.fmt_var.get(), FORMATS_LEVELS["ZIP"])
        self.lvl_combo.config(values=info["levels"])
        self.lvl_combo.current(info["default"])

    def _on_paths_changed(self, paths: list):
        """Atualiza destino sugerido quando a origem muda."""
        if not paths:
            return
        # Pasta-mãe do primeiro item selecionado
        first  = paths[0]
        parent = str(Path(first).parent)
        if not self.dst_entry.get() or self.use_src_dir.get():
            self.dst_entry.set(parent)

    def _toggle_dst(self):
        use = self.use_src_dir.get()
        self.dst_entry.set_state(not use)
        if use:
            paths = self.drop_zone.get_paths()
            if paths:
                # Sempre pasta-mãe (nunca dentro de uma pasta selecionada)
                self.dst_entry.set(str(Path(paths[0]).parent))

    def _pick_files(self):
        """Seleção múltipla de arquivos via diálogo."""
        ps = filedialog.askopenfilenames(title="Selecionar arquivo(s) de origem")
        if ps:
            self.drop_zone.set_paths(list(ps))

    def _pick_dir(self):
        p = filedialog.askdirectory(title="Selecionar diretório de origem")
        if p:
            self.drop_zone.set_paths([p])

    def _pick_dst(self):
        p = filedialog.askdirectory(title="Selecionar pasta de destino")
        if p:
            self.dst_entry.set(p)

    # ── Execução ─────────────────────────────────────────────────────────────
    def _start(self):
        srcs  = self.drop_zone.get_paths()
        fmt   = self.fmt_var.get()
        algos = [a for a, v in self.algo_vars.items() if v.get()]

        # Destino: sempre pasta-mãe do primeiro item (nunca dentro de uma pasta)
        if self.use_src_dir.get():
            dst = str(Path(srcs[0]).parent) if srcs else ""
        else:
            dst = self.dst_entry.get().strip()

        info  = FORMATS_LEVELS[fmt]
        level = info["values"][max(self.lvl_combo.current(), 0)]

        if not srcs:
            messagebox.showerror("Erro", "Nenhuma origem selecionada.")
            return
        if not all(os.path.exists(p) for p in srcs):
            messagebox.showerror("Erro", "Um ou mais caminhos de origem não existem.")
            return
        if not dst:
            messagebox.showerror("Erro", "Defina a pasta de destino.")
            return
        if not algos:
            messagebox.showerror("Erro", "Selecione ao menos um algoritmo de hash.")
            return
        if fmt == "7Z" and not HAS_7Z:
            messagebox.showerror("Erro", "py7zr não instalado.\nExecute: pip install py7zr")
            return

        self.run_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self._clear_result()
        self._report = None

        threading.Thread(target=self._worker,
                         args=(srcs, dst, fmt, level, algos),
                         daemon=True).start()

    # ── Worker ───────────────────────────────────────────────────────────────
    def _worker(self, srcs: list, dst: str, fmt: str, level: int, algos: list):
        ext_map = {"ZIP": ".zip", "TAR.GZ": ".tar.gz",
                   "TAR.BZ2": ".tar.bz2", "7Z": ".7z"}

        # Nome do arquivo: stem do único item, ou "archive_N" para múltiplos
        if len(srcs) == 1:
            stem = Path(srcs[0]).stem
        else:
            stem = f"archive_{len(srcs)}_items"

        dest = os.path.join(dst, stem + ext_map[fmt])

        info      = FORMATS_LEVELS[fmt]
        lvl_idx   = info["values"].index(level) if level in info["values"] else info["default"]
        lvl_label = info["levels"][lvl_idx]

        t0 = time.time()
        try:
            orig_size = total_src_size(srcs)

            # Etapa 1: hashes dos arquivos originais
            self._set_status("Calculando hashes dos arquivos originais…", 2)
            hashes_orig = calc_all_hashes(
                srcs, algos,
                status_cb=lambda msg: self._set_status(msg, 10)
            )

            # Etapa 2: compactação
            self._set_status(f"Compactando {fmt} [{lvl_label}]…", 40)
            compress(srcs, dest, fmt, level,
                     lambda p: self._set_status(
                         f"Compactando {fmt}… {p:.0f}%", 40 + p * 0.3))

            comp_size = os.path.getsize(dest)

            # Etapa 3: hashes do arquivo compactado
            arc_name    = os.path.basename(dest)
            self._set_status(f"Hash do arquivo compactado — {arc_name}…", 72)
            hashes_comp = {arc_name: {}}
            for algo in algos:
                self._set_status(f"Hash {algo} — {arc_name}…", 75)
                hashes_comp[arc_name][algo] = calc_hash(dest, algo)

            duration = round(time.time() - t0, 2)
            ratio    = round((1 - comp_size / orig_size) * 100, 2) if orig_size else 0

            report = {
                "timestamp":             datetime.now().isoformat(timespec="seconds"),
                "sources":               srcs,
                "archive":               dest,
                "format":                fmt,
                "compression_level":     lvl_label,
                "original_size_bytes":   orig_size,
                "compressed_size_bytes": comp_size,
                "reduction_pct":         ratio,
                "duration_sec":          duration,
                "hashes_per_file":       hashes_orig,  # { arquivo: { algo: hex } }
                "hashes_archive":        hashes_comp,  # { arquivo.zip: { algo: hex } }
            }

            self._set_status("✔  Concluído!", 100)
            self.after(0, self._finish, report, dst)

        except Exception as e:
            import traceback; traceback.print_exc()
            self.after(0, messagebox.showerror, "Erro", str(e))
            self.after(0, self._set_status, f"✗  Erro: {e}", 0)
        finally:
            self.after(0, self.run_btn.config, {"state": "normal"})

    # ── Pós-processamento ────────────────────────────────────────────────────
    def _finish(self, report: dict, dst: str):
        self._report = report
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(dst, f"hash_report_{ts}.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            report["_json_saved_to"] = json_path
            self._set_status(
                f"✔  Relatório salvo → {os.path.basename(json_path)}", 100)
        except Exception as e:
            json_path = None
            self._set_status(f"✔  Concluído (erro ao salvar JSON: {e})", 100)

        self._show_result(report, json_path)
        self.export_btn.config(state="normal")

    # ── Exibição de resultado ────────────────────────────────────────────────
    def _show_result(self, report: dict, json_path=None):
        self.result_text.config(state="normal")
        self.result_text.delete("1.0", "end")

        def w(text, tag=None):
            self.result_text.insert("end", text, tag or "value")

        def fmt_bytes(b):
            for u in ("B", "KB", "MB", "GB"):
                if b < 1024: return f"{b:.2f} {u}"
                b /= 1024
            return f"{b:.2f} TB"

        LINE = "═" * 72

        w(LINE + "\n", "header")
        w(f"  RELATÓRIO  ·  {report['timestamp']}\n", "header")
        w(LINE + "\n", "header")

        w("\n  ARQUIVOS\n", "header")
        sources = report.get("sources", [])
        if len(sources) == 1:
            w("  Origem    : ", "label"); w(f"{sources[0]}\n")
        else:
            w(f"  Origens ({len(sources)}):\n", "label")
            for s in sources:
                w(f"    • {s}\n")
        w("  Arquivo   : ", "label"); w(f"{report['archive']}\n")
        w("  Formato   : ", "label")
        w(f"{report['format']}  ·  {report['compression_level']}\n")
        w("  Original  : ", "label")
        w(f"{fmt_bytes(report['original_size_bytes'])}")
        w(f"  ({report['original_size_bytes']:,} bytes)\n", "dim")
        w("  Compacto  : ", "label")
        w(f"{fmt_bytes(report['compressed_size_bytes'])}")
        w(f"  ({report['compressed_size_bytes']:,} bytes)\n", "dim")
        ratio = report["reduction_pct"]
        w("  Redução   : ", "label")
        w(f"{ratio:.2f}%\n", "ok" if ratio > 10 else "warn")
        w("  Duração   : ", "label"); w(f"{report['duration_sec']}s\n")

        if json_path:
            w("\n  RELATÓRIO JSON SALVO AUTOMATICAMENTE\n", "header")
            w(f"  {json_path}\n", "ok")

        # Hashes por arquivo (estrutura: { arquivo: { algo: hex } })
        hashes_per_file = report.get("hashes_per_file", {})
        if hashes_per_file:
            w("\n  HASHES — ARQUIVOS ORIGINAIS\n", "header")
            for fkey, algo_map in hashes_per_file.items():
                label = "HASH COMBINADO" if fkey == "__combined__" else fkey
                w(f"\n  ▸ {label}\n", "file")
                for algo, h in algo_map.items():
                    w(f"    {algo:<10}: ", "label"); w(f"{h}\n")

        w("\n  HASHES — ARQUIVO COMPACTADO\n", "header")
        for fname, algo_map in report.get("hashes_archive", {}).items():
            w(f"\n  ▸ {fname}\n", "file")
            for algo, h in algo_map.items():
                w(f"    {algo:<10}: ", "label"); w(f"{h}\n")

        w("\n" + LINE + "\n", "header")
        self.result_text.config(state="disabled")

    # ── Exportação manual ────────────────────────────────────────────────────
    def _export_manual(self):
        if not self._report:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("Todos", "*.*")],
            initialfile=f"hash_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._report, f, indent=2, ensure_ascii=False)
            messagebox.showinfo("Exportado", f"Relatório salvo em:\n{path}")

    # ── UI helpers ────────────────────────────────────────────────────────────
    def _set_status(self, msg, pct):
        self.after(0, self.status_lbl.config, {"text": msg})
        self.after(0, self.bar.set, pct)

    def _clear_result(self):
        self.result_text.config(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.config(state="disabled")


# ── Estilo ttk ────────────────────────────────────────────────────────────────
def apply_style(root: tk.Tk):
    s = ttk.Style()
    s.theme_use("clam")

    s.configure("TCombobox",
                 fieldbackground=CARD_BG, background=CARD_BG,
                 foreground=TEXT_PRIMARY, bordercolor=BORDER,
                 lightcolor=BORDER, darkcolor=BORDER,
                 arrowcolor=ACCENT_LIGHT, arrowsize=14,
                 selectbackground=CARD_BG, selectforeground=TEXT_PRIMARY,
                 insertcolor=TEXT_PRIMARY, padding=6)
    s.map("TCombobox",
          fieldbackground=[("readonly", CARD_BG), ("disabled", DARK_BG)],
          foreground=[("readonly", TEXT_PRIMARY), ("disabled", DISABLED_FG)],
          background=[("active", CARD_BG), ("readonly", CARD_BG), ("disabled", DARK_BG)],
          bordercolor=[("focus", ACCENT), ("active", ACCENT)],
          arrowcolor=[("disabled", DISABLED_FG), ("readonly", ACCENT_LIGHT),
                      ("active", ACCENT)])

    root.option_add("*TCombobox*Listbox.background",       CARD_BG)
    root.option_add("*TCombobox*Listbox.foreground",       TEXT_PRIMARY)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "white")
    root.option_add("*TCombobox*Listbox.relief",           "flat")
    root.option_add("*TCombobox*Listbox.borderWidth",      "0")

    s.configure("TScrollbar",
                 background=CARD_BG, troughcolor=DARK_BG,
                 bordercolor=DARK_BG, arrowcolor=TEXT_SEC)


if __name__ == "__main__":
    app = App()
    apply_style(app)
    app.mainloop()
