"""E1 de RFC-EXP-00018: contar el arranque de Linux nommu y CoreMark en mini-rv32ima.

Compila ``benchmarks/rv32_arranque/contar.c`` (el núcleo de mini-rv32ima envuelto en
contadores) con clang contra la cabecera de un checkout de mini-rv32ima, arranca la
imagen Linux nommu hasta el prompt del shell (el login ``root`` entra por el UART
cuando aparece ``login:``) y ejecuta los binarios bare-metal de CoreMark de
``benchmarks/rv32_arranque/bin`` (versionados con sus sha256; con ``--coremark`` se
recompilan desde un checkout de CoreMark con el port de ``rv32_arranque/coremark``).
Para cada ejecución imprime instrucciones, longitud dinámica de bloque (corte de
P1.4: salto, CSR, ECALL, MRET, WFI, FENCE, trap o 32 instrucciones), fracción de
escrituras, bloques que terminan tras una escritura y páginas de 4 KiB escritas por
ventana de 2^22 instrucciones, y los veredictos del RFC (§5, E1). La base de tiempo
del invitado va atada al recuento de instrucciones (``-t``: instrucciones por µs de
``mtime``), sin teclado ni reloj del host: cada cifra es determinista.

Fuentes externas, no versionadas (descargar a mano y fijar estos commits):
  mini-rv32ima  https://github.com/cnlohr/mini-rv32ima            commit 84858f58
  Image         linux-6.1.14-rv32nommu-cnl-1.zip de cnlohr/mini-rv32ima-images
                (sha256 del ``Image`` descomprimido: 5f596134705d5aa8…5659e53cbebba6c7e)
  coremark      https://github.com/eembc/coremark                 commit 1f483d5b

Uso (desde la raíz del repositorio):
    python benchmarks/benchmark_rv32_arranque.py --mini-rv32ima DIR --imagen Image [--coremark DIR]
        [--clang clang] [--tiempos 1,4,16,64] [--construir DIR] [--json informe.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E1_DIR = ROOT / "benchmarks" / "rv32_arranque"
BIN_DIR = E1_DIR / "bin"
PORT_DIR = E1_DIR / "coremark"
IMAGE_SHA256 = "5f596134705d5aa8e7c8c406695a560d08faaff4a86bc715659e53cbebba6c7e"
COREMARK_FILES = ("core_list_join.c", "core_main.c", "core_matrix.c", "core_state.c", "core_util.c")
COREMARK_VARIANTS = (("rv32im_zicsr", 10), ("rv32im_zicsr", 20), ("rv32i_zicsr", 10), ("rv32i_zicsr", 20))
LINUX_CAP = 5_000_000_000
# Umbrales del RFC-EXP-00018 §5, E1.
P5_MAX_INSTRUCTIONS = 2_600_000_000      # 4 procesos a 0,55 M instr/s en 20 minutos
P5_MAX_DIRTY_FRACTION = 0.10             # mediana de páginas sucias por ventana sobre la RAM
H3_LINUX_MAX_INSTRUCTIONS = 1_000_000_000


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)


def compile_counter(clang: str, mini_dir: Path, build: Path) -> Path:
    header_dir = mini_dir / "mini-rv32ima"
    if not (header_dir / "mini-rv32ima.h").is_file():
        raise FileNotFoundError(f"No se encuentra mini-rv32ima.h en {header_dir}")
    exe = build / ("contar.exe" if sys.platform == "win32" else "contar")
    run([clang, "-O2", "-D_CRT_SECURE_NO_WARNINGS", f"-I{header_dir}", "-o", str(exe), str(E1_DIR / "contar.c")])
    return exe


def build_coremark(clang: str, coremark_dir: Path, build: Path) -> dict[str, Path]:
    """Compila las cuatro variantes con el port de ``rv32_arranque/coremark``."""
    for name in COREMARK_FILES + ("coremark.h", "barebones/ee_printf.c"):
        if not (coremark_dir / name).is_file():
            raise FileNotFoundError(f"No se encuentra {name} en {coremark_dir}")
    # El ee_printf.c de barebones trae un stub de uart_send_char con #error; el port lo define.
    source = (coremark_dir / "barebones" / "ee_printf.c").read_text(encoding="utf-8").replace("\r\n", "\n")
    patched, count = re.subn(r"void\s*\n\s*uart_send_char\(char c\)\s*\n\{.*?\n\}\n", "void uart_send_char(char c);\n",
                             source, flags=re.S)
    if count != 1:
        raise RuntimeError("No se reconoce el stub uart_send_char de barebones/ee_printf.c")
    ee_printf = build / "ee_printf.c"
    ee_printf.write_text(patched, encoding="utf-8", newline="\n")
    binaries: dict[str, Path] = {}
    for arch, iterations in COREMARK_VARIANTS:
        name = f"coremark_{arch}_it{iterations}"
        elf = build / f"{name}.elf"
        sources = [str(PORT_DIR / "start.S")] + [str(coremark_dir / f) for f in COREMARK_FILES] + [
            str(PORT_DIR / "core_portme.c"), str(ee_printf), str(PORT_DIR / "support.c")]
        if arch.startswith("rv32i_"):
            sources.append(str(PORT_DIR / "builtins_rv32i.c"))
        run([clang, "--target=riscv32-unknown-elf", f"-march={arch}", "-mabi=ilp32", "-O2", "-ffreestanding",
             "-nostdlib", "-fno-builtin", f"-I{coremark_dir}", f"-I{PORT_DIR}", f"-DITERATIONS={iterations}",
             "-DPERFORMANCE_RUN=1", f'-DFLAGS_STR="-O2 -march={arch} -mabi=ilp32"', "-fuse-ld=lld",
             "-T", str(PORT_DIR / "link.ld"), "-o", str(elf), *sources])
        binary = build / f"{name}.bin"
        run(["llvm-objcopy", "-O", "binary", str(elf), str(binary)])
        binaries[name] = binary
    return binaries


def versioned_coremark() -> dict[str, Path]:
    """Binarios versionados; comprueba sus sha256 contra SHA256SUMS."""
    sums = {}
    for line in (BIN_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.strip().partition(" ")
        sums[name.lstrip("*").strip()] = digest
    binaries: dict[str, Path] = {}
    for arch, iterations in COREMARK_VARIANTS:
        name = f"coremark_{arch}_it{iterations}"
        path = BIN_DIR / f"{name}.bin"
        if sha256_of(path) != sums.get(path.name):
            raise RuntimeError(f"sha256 de {path.name} no coincide con SHA256SUMS")
        binaries[name] = path
    return binaries


def count(exe: Path, build: Path, label: str, args: list[str]) -> tuple[dict[str, object], str]:
    report = build / f"{label}.json"
    console = build / f"{label}.txt"
    run([str(exe), *args, "-c", str(LINUX_CAP), "-o", str(console), "-j", str(report)])
    data = json.loads(report.read_text(encoding="utf-8"))
    return data, console.read_text(encoding="utf-8", errors="replace")


def summarize(data: dict[str, object]) -> dict[str, object]:
    n = int(data["instrucciones"])
    classes = data["clases"]
    blocks = int(data["bloques"])
    windows = list(data["paginas_sucias_por_ventana"])
    return {
        "instrucciones": n,
        "parada": data["parada"],
        "instrucciones_que_atrapan": data["instrucciones_que_atrapan"],
        "entradas_en_mtvec": data["entradas_en_mtvec"],
        "fraccion_modo_u": int(data["instrucciones_modo_u"]) / n if n else 0.0,
        "bloque_medio": n / blocks if blocks else 0.0,
        "racha_media": n / int(data["rachas"]) if data["rachas"] else 0.0,
        "bloques_cortados_por_tope": int(data["bloques_cortados_por_tope"]) / blocks if blocks else 0.0,
        "fraccion_stores": (int(classes["store"]) + int(classes["amo"])) / n if n else 0.0,
        "bloques_terminados_tras_store": (int(data["bloques_terminados_tras_store"])
                                          + int(data["bloques_tope_terminados_en_store"])) / blocks if blocks else 0.0,
        "saltos_tomados": (int(data["saltos_condicionales_tomados"]) / int(data["saltos_condicionales_vistos"])
                           if data["saltos_condicionales_vistos"] else 0.0),
        "mezcla": {name: int(value) / n for name, value in classes.items()} if n else {},
        "ventanas": len(windows),
        "paginas_sucias_mediana": statistics.median(windows) if windows else data["paginas_sucias_ventana_parcial"],
        "paginas_sucias_max": max(windows) if windows else data["paginas_sucias_ventana_parcial"],
        "paginas_sucias_total": data["paginas_sucias_total"],
        "paginas_ram": data["paginas_ram"],
    }


def print_summary(title: str, summary: dict[str, object]) -> None:
    print(f"\n{title}")
    print(f"  instrucciones: {summary['instrucciones']:,} (parada: {summary['parada']}; "
          f"traps síncronos: {summary['instrucciones_que_atrapan']:,}; entradas en mtvec: {summary['entradas_en_mtvec']:,}; "
          f"modo U: {summary['fraccion_modo_u']:.1%})")
    print(f"  bloque medio (tope 32): {summary['bloque_medio']:.2f}; racha media sin tope: {summary['racha_media']:.2f}; "
          f"bloques cortados por el tope: {summary['bloques_cortados_por_tope']:.1%}")
    print(f"  escrituras (store+amo): {summary['fraccion_stores']:.1%} de las instrucciones; "
          f"bloques que terminan tras una escritura: {summary['bloques_terminados_tras_store']:.1%}; "
          f"saltos condicionales tomados: {summary['saltos_tomados']:.1%}")
    mix = ", ".join(f"{name} {value:.1%}" for name, value in summary["mezcla"].items() if value >= 0.001)
    print(f"  mezcla: {mix}")
    print(f"  páginas de 4 KiB escritas: {summary['paginas_sucias_total']:,} en total; por ventana de 2^22: "
          f"mediana {summary['paginas_sucias_mediana']}, máximo {summary['paginas_sucias_max']} "
          f"({summary['ventanas']} ventanas completas; RAM de {summary['paginas_ram']:,} páginas)")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="E1 de RFC-EXP-00018: arranque de Linux nommu y CoreMark en mini-rv32ima")
    parser.add_argument("--mini-rv32ima", type=Path, required=True, help="checkout de cnlohr/mini-rv32ima")
    parser.add_argument("--imagen", type=Path, required=True, help="Image de Linux nommu")
    parser.add_argument("--coremark", type=Path, default=None, help="checkout de eembc/coremark para recompilar")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--tiempos", default="1,4,16,64", help="instrucciones por µs de mtime en cada arranque")
    parser.add_argument("--construir", type=Path, default=None, help="directorio de construcción (temporal si falta)")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    clang = shutil.which(args.clang)
    if clang is None:
        print(f"No se encuentra {args.clang}; E1 necesita clang para compilar contar.c", file=sys.stderr)
        return 2
    if not args.imagen.is_file():
        print(f"No se encuentra la imagen {args.imagen}", file=sys.stderr)
        return 2
    image_sha = sha256_of(args.imagen)
    build = args.construir or Path(tempfile.mkdtemp(prefix="rv32_arranque_"))
    build.mkdir(parents=True, exist_ok=True)
    try:
        exe = compile_counter(clang, args.mini_rv32ima, build)
        binaries = build_coremark(clang, args.coremark, build) if args.coremark else versioned_coremark()
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        print(f"Construcción fallida: {exc}\n{detail}", file=sys.stderr)
        return 2

    report: dict[str, object] = {
        "imagen_sha256": image_sha, "imagen_esperada": IMAGE_SHA256, "clang": run([clang, "--version"]).stdout.splitlines()[0],
        "linux": {}, "coremark": {},
    }
    print(f"Imagen {args.imagen.name}: sha256 {image_sha[:16]}… "
          f"({'la de referencia' if image_sha == IMAGE_SHA256 else 'DISTINTA de la de referencia'})")
    for divisor in (int(t) for t in args.tiempos.split(",")):
        data, console = count(exe, build, f"linux_t{divisor}",
                              ["-f", str(args.imagen), "-l", "login: ", "-u", "root\n", "-s", "# ", "-t", str(divisor)])
        summary = summarize(data)
        summary["shell"] = console.rstrip().endswith("#")
        report["linux"][str(divisor)] = summary
        print_summary(f"Linux nommu hasta el shell, {divisor} instrucción(es) por µs de mtime:", summary)
    for name, binary in binaries.items():
        data, console = count(exe, build, name, ["-f", str(binary), "-b", "disable"])
        summary = summarize(data)
        summary["validado"] = "Correct operation validated" in console
        crcs = dict(re.findall(r"\[0\](crc\w+)\s*:\s*(0x[0-9a-f]+)", console))
        summary["crc"] = crcs
        summary["sha256"] = sha256_of(binary)
        report["coremark"][name] = summary
        print_summary(f"CoreMark {name} (sha256 {summary['sha256'][:16]}…):", summary)
        print(f"  CRC: {crcs}; {'validado' if summary['validado'] else 'sin validar (regla de 10 s o CRC)'}")
    for arch in ("rv32im_zicsr", "rv32i_zicsr"):
        ten = int(report["coremark"][f"coremark_{arch}_it10"]["instrucciones"])
        twenty = int(report["coremark"][f"coremark_{arch}_it20"]["instrucciones"])
        report["coremark"][f"{arch}_por_iteracion"] = (twenty - ten) / 10
        print(f"CoreMark {arch}: {(twenty - ten) / 10:,.0f} instrucciones por iteración; {ten - (twenty - ten):,.0f} fijas")

    boots = [int(s["instrucciones"]) for s in report["linux"].values()]
    dirty = [float(s["paginas_sucias_mediana"]) / int(s["paginas_ram"]) for s in report["linux"].values()]
    verdict = {
        "arranque_max": max(boots), "arranque_min": min(boots), "paginas_sucias_fraccion_max": max(dirty),
        "p5_completo_sobrevive": max(boots) <= P5_MAX_INSTRUCTIONS and max(dirty) <= P5_MAX_DIRTY_FRACTION,
        "h3_mantiene_linux": max(boots) <= H3_LINUX_MAX_INSTRUCTIONS,
        "shell_alcanzado": all(bool(s["shell"]) for s in report["linux"].values()),
    }
    report["veredictos"] = verdict
    print(f"\nArranque hasta el shell: {min(boots):,} … {max(boots):,} instrucciones según la base de tiempo; "
          f"shell alcanzado en todos: {verdict['shell_alcanzado']}")
    print(f"P5 completo (umbral {P5_MAX_INSTRUCTIONS:,} instrucciones y {P5_MAX_DIRTY_FRACTION:.0%} de la RAM por ventana): "
          f"{'sobrevive' if verdict['p5_completo_sobrevive'] else 'MUERE'} (páginas sucias {max(dirty):.2%})")
    print(f"H3 (umbral {H3_LINUX_MAX_INSTRUCTIONS:,}): {'mantiene Linux nommu' if verdict['h3_mantiene_linux'] else 'SE REBAJA a Zephyr y FreeRTOS'}")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
