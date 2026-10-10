// E1 de RFC-EXP-00018: mini-rv32ima instrumentado para contar el arranque.
//
// Envuelve el núcleo mini-rv32ima.h (cnlohr, MIT/BSD/CC0) con contadores:
// instrucciones ejecutadas (y las que atrapan), mezcla por clase, saltos
// tomados, bloques básicos según el corte de P1.4 (salto, CSR, ECALL, MRET,
// WFI, FENCE, trap, o 32 instrucciones), rachas sin tope, fracción de
// escrituras y de bloques que terminan en escritura, y páginas de 4 KiB
// escritas por ventana de 2^22 instrucciones (P5.2). La base de tiempo va
// atada al recuento de instrucciones (como `-l` del emulador original), el
// teclado solo entrega el diálogo programado y no se dormita en WFI: la
// ejecución es determinista.
//
// Compilar:  clang -O2 -I<mini-rv32ima/mini-rv32ima> -o contar.exe contar.c
// Uso:       contar.exe -f Image [-m bytes] [-k cmdline] [-b dtb|disable]
//                       [-c max_instr] [-s marcador] [-l marcador -u texto]
//                       [-o consola.txt] [-j informe.json] [-t divisor] [-i N] [-e]
//   -s  detiene la cuenta cuando la consola emite el marcador (si hay -u, solo
//       después de haber enviado el texto)
//   -l/-u  cuando la consola emite el marcador -l, el UART entrega el texto -u
//   -i  detiene la cuenta tras N sondeos WFI consecutivos sin instrucciones
//   -e  reenvía la consola del invitado a stdout además del archivo

#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "default64mbdtc.h"

#define WINDOW_SHIFT 22
#define MAX_WINDOWS 16384
#define PAGE_SHIFT 12
#define BLOCK_CAP 32
#define RUN_HIST 1024
#define TAIL 256

static uint32_t ram_amt = 64 * 1024 * 1024;
static uint8_t *ram_image;
static struct MiniRV32IMAState *core;
static FILE *console_file;
static int echo_console;
static const char *marker;
static size_t marker_len;
static const char *dialog_marker, *dialog_text;
static size_t dialog_marker_len, dialog_pos;
static int dialog_active, dialog_sent;
static char tail[TAIL];
static int stop_requested;
static const char *stop_reason = "limite";

// Páginas sucias por ventana (P5.2) y en total.
static uint8_t *dirty_window;
static uint8_t *dirty_ever;
static uint32_t pages_total;
static uint32_t dirty_window_count, dirty_ever_count;
static uint32_t window_pages[MAX_WINDOWS];
static int windows;

static inline void mark_dirty(uint32_t ofs)
{
	uint32_t page = ofs >> PAGE_SHIFT;
	if (!dirty_window[page]) { dirty_window[page] = 1; dirty_window_count++; }
	if (!dirty_ever[page]) { dirty_ever[page] = 1; dirty_ever_count++; }
}

#define MINIRV32_CUSTOM_MEMORY_BUS
#define MINIRV32_STORE4(ofs, val) do { mark_dirty(ofs); *(uint32_t *)(image + (ofs)) = (val); } while (0)
#define MINIRV32_STORE2(ofs, val) do { mark_dirty(ofs); *(uint16_t *)(image + (ofs)) = (val); } while (0)
#define MINIRV32_STORE1(ofs, val) do { mark_dirty(ofs); *(uint8_t *)(image + (ofs)) = (val); } while (0)
#define MINIRV32_LOAD4(ofs) (*(uint32_t *)(image + (ofs)))
#define MINIRV32_LOAD2(ofs) (*(uint16_t *)(image + (ofs)))
#define MINIRV32_LOAD1(ofs) (*(uint8_t *)(image + (ofs)))
#define MINIRV32_LOAD2_SIGNED(ofs) (*(int16_t *)(image + (ofs)))
#define MINIRV32_LOAD1_SIGNED(ofs) (*(int8_t *)(image + (ofs)))

// Contadores.
enum { C_ALU, C_ALU_M, C_LOAD, C_STORE, C_AMO, C_BRANCH, C_JAL, C_JALR, C_LUI_AUIPC,
       C_FENCE, C_CSR, C_ECALL_EBREAK, C_MRET, C_WFI, C_OTHER, C_COUNT };
static const char *class_names[C_COUNT] = {
	"alu", "alu_m", "load", "store", "amo", "branch", "jal", "jalr", "lui_auipc",
	"fence", "csr", "ecall_ebreak", "mret", "wfi", "otra" };
static uint64_t n_instr, n_trap_instr, n_umode, n_mtvec_entries;
static uint64_t n_class[C_COUNT];
static uint64_t n_branch_known, n_branch_taken;
static uint64_t n_blocks, blocks_cap, blocks_end_store, cap_end_store, block_len;
static uint64_t block_hist[BLOCK_CAP + 1];
static uint64_t n_runs, run_len, run_hist[RUN_HIST + 1];
static int prev_was_store;
static uint32_t expected_pc;
static int expected_known;
static uint64_t wfi_polls;
static uint64_t max_instr = 0;
static uint64_t idle_limit = 0;

static void Count(uint32_t pc, uint32_t ir, uint32_t trap);
static uint32_t HandleControlStore(uint32_t addy, uint32_t val);
static uint32_t HandleControlLoad(uint32_t addy);
static void HandleOtherCSRWrite(uint8_t *image, uint16_t csrno, uint32_t value);
static int32_t HandleOtherCSRRead(uint8_t *image, uint16_t csrno);

#define MINIRV32WARN(x...) fprintf(stderr, x);
#define MINIRV32_DECORATE static
#define MINI_RV32_RAM_SIZE ram_amt
#define MINIRV32_IMPLEMENTATION
#define MINIRV32_POSTEXEC(pc, ir, retval) { Count(pc, ir, retval); }
#define MINIRV32_HANDLE_MEM_STORE_CONTROL(addy, val) if (HandleControlStore(addy, val)) return val;
#define MINIRV32_HANDLE_MEM_LOAD_CONTROL(addy, rval) rval = HandleControlLoad(addy);
#define MINIRV32_OTHERCSR_WRITE(csrno, value) HandleOtherCSRWrite(image, csrno, value);
#define MINIRV32_OTHERCSR_READ(csrno, value) value = HandleOtherCSRRead(image, csrno);

#include "mini-rv32ima.h"

static void Count(uint32_t pc, uint32_t ir, uint32_t trap)
{
	n_instr++;
	if ((core->extraflags & 3) == 0) n_umode++;
	uint32_t op = ir & 0x7f;
	int cls, is_store = 0, terminator = 0;
	switch (op) {
	case 0x63: cls = C_BRANCH; terminator = 1; break;
	case 0x6F: cls = C_JAL; terminator = 1; break;
	case 0x67: cls = C_JALR; terminator = 1; break;
	case 0x03: cls = C_LOAD; break;
	case 0x23: cls = C_STORE; is_store = 1; break;
	case 0x2F: cls = C_AMO; is_store = (((ir >> 27) & 0x1f) != 2); break; // LR.W no escribe
	case 0x13: cls = C_ALU; break;
	case 0x33: cls = (ir & 0x02000000) ? C_ALU_M : C_ALU; break;
	case 0x37: case 0x17: cls = C_LUI_AUIPC; break;
	case 0x0F: cls = C_FENCE; terminator = 1; break;
	case 0x73: {
		uint32_t f3 = (ir >> 12) & 7, csr = ir >> 20;
		if (f3 & 3) cls = C_CSR;
		else if ((csr & 0xff) == 0x02) cls = C_MRET;
		else if (csr == 0x105) cls = C_WFI;
		else cls = C_ECALL_EBREAK;
		terminator = 1;
		break;
	}
	default: cls = C_OTHER;
	}
	n_class[cls]++;
	if (trap) { n_trap_instr++; terminator = 1; }
	// Para las instrucciones que no saltan, pc es la dirección de la instrucción; para un salto
	// tomado es destino - 4. La esperada viene de la instrucción anterior (pc + 4).
	int control = (op == 0x63 || op == 0x6F || op == 0x67 || cls == C_MRET);
	uint32_t this_pc = (control && expected_known) ? expected_pc : pc;
	if (core->mtvec && this_pc == core->mtvec) n_mtvec_entries++;
	if (op == 0x63 && !trap && expected_known) {
		n_branch_known++;
		if (pc != expected_pc) n_branch_taken++;
	}
	if (trap) expected_known = 0;
	else { expected_pc = pc + 4; expected_known = 1; }

	block_len++;
	run_len++;
	if (terminator) {
		n_blocks++;
		block_hist[block_len]++;
		if (prev_was_store) blocks_end_store++;
		block_len = 0;
		prev_was_store = 0;
		n_runs++;
		run_hist[run_len > RUN_HIST ? RUN_HIST : run_len]++;
		run_len = 0;
	} else if (block_len == BLOCK_CAP) {
		n_blocks++;
		blocks_cap++;
		block_hist[BLOCK_CAP]++;
		if (is_store) cap_end_store++;
		block_len = 0;
		prev_was_store = 0;
	} else {
		prev_was_store = is_store;
	}
	if ((n_instr & ((1u << WINDOW_SHIFT) - 1)) == 0) {
		if (windows < MAX_WINDOWS) window_pages[windows++] = dirty_window_count;
		memset(dirty_window, 0, pages_total);
		dirty_window_count = 0;
	}
	if (max_instr && n_instr >= max_instr) { stop_requested = 1; stop_reason = "limite"; }
}

static int tail_ends_with(const char *text, size_t len)
{
	return len && len <= TAIL && memcmp(tail + TAIL - len, text, len) == 0;
}

static void console_char(char c)
{
	if (console_file) fputc(c, console_file);
	if (echo_console) { fputc(c, stdout); fflush(stdout); }
	memmove(tail, tail + 1, TAIL - 1);
	tail[TAIL - 1] = c;
	if (dialog_marker_len && !dialog_sent && !dialog_active && tail_ends_with(dialog_marker, dialog_marker_len))
		dialog_active = 1;
	if (marker_len && (!dialog_text || dialog_sent) && tail_ends_with(marker, marker_len)) {
		stop_requested = 1;
		stop_reason = "marcador";
	}
}

static int keyboard_ready(void)
{
	return dialog_active && dialog_text && dialog_text[dialog_pos] != 0;
}

static int keyboard_read(void)
{
	if (!keyboard_ready()) return -1;
	int c = (unsigned char)dialog_text[dialog_pos++];
	if (dialog_text[dialog_pos] == 0) { dialog_active = 0; dialog_sent = 1; }
	return c;
}

static uint32_t HandleControlStore(uint32_t addy, uint32_t val)
{
	if (addy == 0x10000000) console_char((char)val);        // UART 8250: registro de datos
	else if (addy == 0x11004004) core->timermatchh = val;   // CLINT mtimecmp alto
	else if (addy == 0x11004000) core->timermatchl = val;   // CLINT mtimecmp bajo
	else if (addy == 0x11100000) { core->pc = core->pc + 4; return val; } // SYSCON
	return 0;
}

static uint32_t HandleControlLoad(uint32_t addy)
{
	if (addy == 0x10000005) return 0x60 | (keyboard_ready() ? 1 : 0); // LSR: THR vacío, DR si hay diálogo
	else if (addy == 0x10000000 && keyboard_ready()) return (uint32_t)keyboard_read();
	else if (addy == 0x1100bffc) return core->timerh;
	else if (addy == 0x1100bff8) return core->timerl;
	return 0;
}

static void HandleOtherCSRWrite(uint8_t *image, uint16_t csrno, uint32_t value)
{
	if (csrno == 0x136) { char b[16]; int n = snprintf(b, sizeof b, "%d", value); for (int i = 0; i < n; i++) console_char(b[i]); }
	else if (csrno == 0x137) { char b[16]; int n = snprintf(b, sizeof b, "%08x", value); for (int i = 0; i < n; i++) console_char(b[i]); }
	else if (csrno == 0x138) {
		uint32_t ptr = value - MINIRV32_RAM_IMAGE_OFFSET;
		while (ptr < ram_amt && image[ptr]) console_char((char)image[ptr++]);
	}
	else if (csrno == 0x139) console_char((char)value);
}

static int32_t HandleOtherCSRRead(uint8_t *image, uint16_t csrno)
{
	(void)image;
	if (csrno == 0x140) return keyboard_ready() ? keyboard_read() : -1;
	return 0;
}

static int64_t ReadNumber(const char *number, int64_t fallback)
{
	if (!number || !number[0]) return fallback;
	char *end;
	long long v = strtoll(number, &end, 0);
	return end == number ? fallback : v;
}

static uint32_t median_u32(const uint32_t *values, int count)
{
	if (count <= 0) return 0;
	uint32_t *copy = malloc(sizeof(uint32_t) * count);
	memcpy(copy, values, sizeof(uint32_t) * count);
	for (int i = 1; i < count; i++) { uint32_t v = copy[i]; int j = i - 1; while (j >= 0 && copy[j] > v) { copy[j + 1] = copy[j]; j--; } copy[j + 1] = v; }
	uint32_t m = (count & 1) ? copy[count / 2] : (copy[count / 2 - 1] + copy[count / 2]) / 2;
	free(copy);
	return m;
}

static void write_json_string(FILE *out, const char *text)
{
	fputc('"', out);
	for (; *text; text++) {
		unsigned char c = (unsigned char)*text;
		if (c == '"' || c == '\\') { fputc('\\', out); fputc(c, out); }
		else if (c < 0x20) fprintf(out, "\\u%04x", c);
		else fputc(c, out);
	}
	fputc('"', out);
}

static void write_report(FILE *out, const char *image_name, uint64_t cycles)
{
	fprintf(out, "{\n  \"imagen\": ");
	write_json_string(out, image_name);
	fprintf(out, ",\n  \"ram_bytes\": %u,\n  \"paginas_ram\": %u,\n", ram_amt, pages_total);
	fprintf(out, "  \"parada\": \"%s\",\n  \"instrucciones\": %llu,\n  \"instrucciones_que_atrapan\": %llu,\n",
		stop_reason, (unsigned long long)n_instr, (unsigned long long)n_trap_instr);
	fprintf(out, "  \"entradas_en_mtvec\": %llu,\n  \"instrucciones_modo_u\": %llu,\n  \"ciclos_invitado\": %llu,\n  \"sondeos_wfi\": %llu,\n",
		(unsigned long long)n_mtvec_entries, (unsigned long long)n_umode, (unsigned long long)cycles, (unsigned long long)wfi_polls);
	fprintf(out, "  \"clases\": {");
	for (int i = 0; i < C_COUNT; i++) fprintf(out, "%s\"%s\": %llu", i ? ", " : "", class_names[i], (unsigned long long)n_class[i]);
	fprintf(out, "},\n");
	fprintf(out, "  \"saltos_condicionales_vistos\": %llu,\n  \"saltos_condicionales_tomados\": %llu,\n",
		(unsigned long long)n_branch_known, (unsigned long long)n_branch_taken);
	fprintf(out, "  \"bloques\": %llu,\n  \"bloques_cortados_por_tope\": %llu,\n  \"bloques_terminados_tras_store\": %llu,\n  \"bloques_tope_terminados_en_store\": %llu,\n",
		(unsigned long long)n_blocks, (unsigned long long)blocks_cap, (unsigned long long)blocks_end_store, (unsigned long long)cap_end_store);
	fprintf(out, "  \"histograma_bloques\": [");
	for (int i = 1; i <= BLOCK_CAP; i++) fprintf(out, "%s%llu", i > 1 ? ", " : "", (unsigned long long)block_hist[i]);
	fprintf(out, "],\n");
	fprintf(out, "  \"rachas\": %llu,\n  \"histograma_rachas\": [", (unsigned long long)n_runs);
	for (int i = 1; i <= RUN_HIST; i++) fprintf(out, "%s%llu", i > 1 ? ", " : "", (unsigned long long)run_hist[i]);
	fprintf(out, "],\n");
	fprintf(out, "  \"paginas_sucias_total\": %u,\n  \"ventanas\": %d,\n  \"paginas_sucias_por_ventana\": [", dirty_ever_count, windows);
	for (int i = 0; i < windows; i++) fprintf(out, "%s%u", i ? ", " : "", window_pages[i]);
	fprintf(out, "],\n  \"paginas_sucias_ventana_parcial\": %u,\n", dirty_window_count);
	fprintf(out, "  \"paginas_sucias_mediana\": %u\n}\n", median_u32(window_pages, windows));
}

int main(int argc, char **argv)
{
	const char *image_file_name = 0, *dtb_file_name = 0, *kernel_command_line = 0;
	const char *console_name = 0, *json_name = 0;
	int time_divisor = 1;
	for (int i = 1; i < argc; i++) {
		const char *p = argv[i];
		if (p[0] != '-' || !p[1]) { fprintf(stderr, "parametro no reconocido: %s\n", p); return 1; }
		switch (p[1]) {
		case 'm': ram_amt = (uint32_t)ReadNumber(++i < argc ? argv[i] : 0, ram_amt); break;
		case 'c': max_instr = (uint64_t)ReadNumber(++i < argc ? argv[i] : 0, 0); break;
		case 'k': kernel_command_line = ++i < argc ? argv[i] : 0; break;
		case 'f': image_file_name = ++i < argc ? argv[i] : 0; break;
		case 'b': dtb_file_name = ++i < argc ? argv[i] : 0; break;
		case 's': marker = ++i < argc ? argv[i] : 0; marker_len = marker ? strlen(marker) : 0; break;
		case 'l': dialog_marker = ++i < argc ? argv[i] : 0; dialog_marker_len = dialog_marker ? strlen(dialog_marker) : 0; break;
		case 'u': dialog_text = ++i < argc ? argv[i] : 0; break;
		case 'o': console_name = ++i < argc ? argv[i] : 0; break;
		case 'j': json_name = ++i < argc ? argv[i] : 0; break;
		case 't': time_divisor = (int)ReadNumber(++i < argc ? argv[i] : 0, 1); break;
		case 'i': idle_limit = (uint64_t)ReadNumber(++i < argc ? argv[i] : 0, 0); break;
		case 'e': echo_console = 1; break;
		default: fprintf(stderr, "parametro no reconocido: %s\n", p); return 1;
		}
	}
	if (!image_file_name || time_divisor <= 0 || (dialog_text && !dialog_marker_len)) {
		fprintf(stderr, "uso: contar -f Image [-m bytes] [-k cmdline] [-b dtb|disable] [-c max] [-s marcador] [-l marcador -u texto] [-o consola] [-j json] [-t divisor] [-i sondeos_wfi] [-e]\n");
		return 1;
	}
	ram_image = malloc(ram_amt);
	pages_total = ram_amt >> PAGE_SHIFT;
	dirty_window = calloc(pages_total, 1);
	dirty_ever = calloc(pages_total, 1);
	if (!ram_image || !dirty_window || !dirty_ever) { fprintf(stderr, "sin memoria\n"); return 2; }
	memset(ram_image, 0, ram_amt);
	FILE *f = fopen(image_file_name, "rb");
	if (!f) { fprintf(stderr, "no se encuentra %s\n", image_file_name); return 3; }
	fseek(f, 0, SEEK_END);
	long flen = ftell(f);
	fseek(f, 0, SEEK_SET);
	if (flen <= 0 || (uint32_t)flen > ram_amt || fread(ram_image, flen, 1, f) != 1) { fprintf(stderr, "imagen invalida\n"); return 3; }
	fclose(f);
	uint32_t dtb_ptr = 0;
	if (dtb_file_name && strcmp(dtb_file_name, "disable") == 0) {
		// sin DTB
	} else if (dtb_file_name) {
		f = fopen(dtb_file_name, "rb");
		if (!f) { fprintf(stderr, "no se encuentra %s\n", dtb_file_name); return 3; }
		fseek(f, 0, SEEK_END);
		long dtblen = ftell(f);
		fseek(f, 0, SEEK_SET);
		dtb_ptr = ram_amt - dtblen - sizeof(struct MiniRV32IMAState);
		if (fread(ram_image + dtb_ptr, dtblen, 1, f) != 1) { fprintf(stderr, "dtb invalido\n"); return 3; }
		fclose(f);
	} else {
		dtb_ptr = ram_amt - sizeof(default64mbdtb) - sizeof(struct MiniRV32IMAState);
		memcpy(ram_image + dtb_ptr, default64mbdtb, sizeof(default64mbdtb));
		if (kernel_command_line) strncpy((char *)(ram_image + dtb_ptr + 0xc0), kernel_command_line, 54);
		uint32_t *dtb = (uint32_t *)(ram_image + dtb_ptr);
		if (dtb[0x13c / 4] == 0x00c0ff03) {
			uint32_t validram = dtb_ptr;
			dtb[0x13c / 4] = (validram >> 24) | (((validram >> 16) & 0xff) << 8) | (((validram >> 8) & 0xff) << 16) | ((validram & 0xff) << 24);
		}
	}
	if (console_name) console_file = fopen(console_name, "wb");

	core = (struct MiniRV32IMAState *)(ram_image + ram_amt - sizeof(struct MiniRV32IMAState));
	core->pc = MINIRV32_RAM_IMAGE_OFFSET;
	core->regs[10] = 0x00;
	core->regs[11] = dtb_ptr ? (dtb_ptr + MINIRV32_RAM_IMAGE_OFFSET) : 0;
	core->extraflags |= 3;

	uint64_t lastTime = 0;
	uint64_t idle_streak = 0;
	const int instrs_per_flip = 1024;
	while (!stop_requested) {
		uint64_t *this_ccount = (uint64_t *)&core->cyclel;
		uint32_t elapsedUs = (uint32_t)(*this_ccount / time_divisor - lastTime);
		lastTime += elapsedUs;
		uint64_t before = n_instr;
		int ret = MiniRV32IMAStep(core, ram_image, 0, elapsedUs, instrs_per_flip);
		if (n_instr == before) {
			idle_streak++;
			if (idle_limit && idle_streak >= idle_limit) { stop_requested = 1; stop_reason = "ocioso"; }
		} else idle_streak = 0;
		switch (ret) {
		case 0: break;
		case 1: wfi_polls++; *this_ccount += instrs_per_flip; break;
		case 0x7777: fprintf(stderr, "reinicio solicitado por el invitado\n"); stop_requested = 1; stop_reason = "reinicio"; break;
		case 0x5555: stop_requested = 1; stop_reason = "apagado"; break;
		default: fprintf(stderr, "fallo desconocido %d\n", ret); stop_requested = 1; stop_reason = "fallo"; break;
		}
	}
	uint64_t cycles = ((uint64_t)core->cycleh << 32) | core->cyclel;
	if (console_file) fclose(console_file);
	if (json_name) {
		FILE *j = fopen(json_name, "wb");
		if (j) { write_report(j, image_file_name, cycles); fclose(j); }
	}
	fprintf(stderr, "parada=%s instrucciones=%llu atrapan=%llu bloques=%llu media_bloque=%.3f rachas_media=%.3f stores=%llu amo=%llu ventanas=%d mediana_paginas=%u total_paginas_sucias=%u\n",
		stop_reason, (unsigned long long)n_instr, (unsigned long long)n_trap_instr, (unsigned long long)n_blocks,
		n_blocks ? (double)n_instr / (double)n_blocks : 0.0, n_runs ? (double)n_instr / (double)n_runs : 0.0,
		(unsigned long long)n_class[C_STORE], (unsigned long long)n_class[C_AMO], windows, median_u32(window_pages, windows), dirty_ever_count);
	return 0;
}
