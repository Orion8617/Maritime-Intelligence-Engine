// ═══════════════════════════════════════════════════════════════════════════
// MAYA ISA — Virtual Machine Kernel (Rust → WebAssembly)
// 16-bit instruction format basado en matemática vigesimal Maya:
//   [opcode:5][target:5][data:6]   (15..11 | 10..6 | 5..0)
// Decodificación bitwise pura para velocidad nativa en CPU.
// ═══════════════════════════════════════════════════════════════════════════

use wasm_bindgen::prelude::*;
use serde::Serialize;

// ── Opcodes (5 bits → 32 valores posibles) ─────────────────────────────────
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MayaOp {
    NOP   = 0x00,  // 🐚 (Maya zero / concha) — no-op
    FIRE  = 0x01,  // •     forzar spike en neurona[target] = 30.0
    INHIB = 0x02,  // ••    inhibir: v[target] -= data
    RESET = 0x03,  // •••   v[target] = 0
    DECAY = 0x04,  // ••••  v[target] *= (1 - data/63)
    INTEG = 0x05,  // ━     v[target] += data (integrar input)
    THRES = 0x06,  // ━•    si v[target] > data → spike + reset
    LEAK  = 0x07,  // ━••   v[target] *= 0.95 (leak constante)
    WPOT  = 0x08,  // ━•••  potenciar peso[target] += data (clamp 63)
    WDEP  = 0x09,  // ━•••• deprimir peso[target] -= data (clamp 0)
    WCLR  = 0x0A,  // ━━    peso[target] = 0
    WSET  = 0x0B,  // ━━•   peso[target] = data
    STDP  = 0x0C,  // ━━••  Spike-Timing Dependent Plasticity (Hebbian)
    SBASE = 0x0D,  // ━━••• base_address = target * 32 + data (rebase ventana)
    JMP   = 0x0E,  // ━━━   pc = target * 64 + data (salto absoluto)
    JNZ   = 0x0F,  // ━━━•  si v[target] != 0 → saltar +data instrucciones
    BATCH = 0x17,  // 0x17 instrucción extendida (data = sub-opcode)
    HALT  = 0x1F,  // 🐚🐚🐚🐚🐚 fin de ejecución
}

impl MayaOp {
    #[inline(always)]
    pub fn from_u8(v: u8) -> u8 { v & 0x1F }
}

// ── VirtualCore ────────────────────────────────────────────────────────────
#[wasm_bindgen]
pub struct VirtualCore {
    base_address: usize,
    neurons: Vec<f32>,
    weights: Vec<u8>,        // 6-bit quantized (0..63)
    spike_count: u32,
    cycles_executed: u32,
    halted: bool,
}

#[derive(Serialize)]
pub struct ExecResult {
    pub cycles: u32,
    pub spikes: u32,
    pub halted: bool,
    pub final_pc: u32,
    pub neurons_active: u32,
    pub avg_voltage: f32,
    pub avg_weight: f32,
    pub max_voltage: f32,
}

#[wasm_bindgen]
impl VirtualCore {
    #[wasm_bindgen(constructor)]
    pub fn new(num_neurons: usize, num_weights: usize) -> VirtualCore {
        VirtualCore {
            base_address: 0,
            neurons: vec![0.0_f32; num_neurons.max(32)],
            weights: vec![0_u8; num_weights.max(32)],
            spike_count: 0,
            cycles_executed: 0,
            halted: false,
        }
    }

    /// Reinicia el estado pero conserva los pesos (preserva el "ADN neuronal")
    pub fn reset_voltages(&mut self) {
        for v in self.neurons.iter_mut() { *v = 0.0; }
        self.spike_count = 0;
        self.cycles_executed = 0;
        self.halted = false;
        self.base_address = 0;
    }

    /// Reset total
    pub fn reset_all(&mut self) {
        self.reset_voltages();
        for w in self.weights.iter_mut() { *w = 0; }
    }

    pub fn neuron_count(&self) -> usize { self.neurons.len() }
    pub fn weight_count(&self) -> usize { self.weights.len() }
    pub fn spike_count(&self) -> u32 { self.spike_count }
    pub fn cycles(&self) -> u32 { self.cycles_executed }
    pub fn is_halted(&self) -> bool { self.halted }

    /// Lectura para debugging desde JS
    pub fn get_voltage(&self, idx: usize) -> f32 {
        *self.neurons.get(idx).unwrap_or(&0.0)
    }
    pub fn get_weight(&self, idx: usize) -> u8 {
        *self.weights.get(idx).unwrap_or(&0)
    }

    /// Inyectar voltaje (para tests / inicialización)
    pub fn set_voltage(&mut self, idx: usize, v: f32) {
        if idx < self.neurons.len() { self.neurons[idx] = v; }
    }
    pub fn set_weight(&mut self, idx: usize, w: u8) {
        if idx < self.weights.len() { self.weights[idx] = w.min(63); }
    }

    /// Ejecuta un Command Buffer (Uint16Array desde JS) a velocidad nativa.
    /// Devuelve JSON con métricas de ejecución.
    pub fn execute(&mut self, instruction_buffer: &[u16]) -> String {
        self.execute_internal(instruction_buffer);
        self.metrics_json()
    }

    /// Versión sin retorno JSON (más rápida si solo se quieren los efectos)
    pub fn execute_fast(&mut self, instruction_buffer: &[u16]) {
        self.execute_internal(instruction_buffer);
    }

    pub fn metrics_json(&self) -> String {
        let active = self.neurons.iter().filter(|v| **v > 0.001 || **v < -0.001).count() as u32;
        let avg_v = if !self.neurons.is_empty() {
            self.neurons.iter().sum::<f32>() / self.neurons.len() as f32
        } else { 0.0 };
        let avg_w = if !self.weights.is_empty() {
            self.weights.iter().map(|w| *w as f32).sum::<f32>() / self.weights.len() as f32
        } else { 0.0 };
        let max_v = self.neurons.iter().cloned().fold(f32::MIN, f32::max);
        let r = ExecResult {
            cycles: self.cycles_executed,
            spikes: self.spike_count,
            halted: self.halted,
            final_pc: self.cycles_executed,
            neurons_active: active,
            avg_voltage: avg_v,
            avg_weight: avg_w,
            max_voltage: if max_v == f32::MIN { 0.0 } else { max_v },
        };
        serde_json::to_string(&r).unwrap_or_else(|_| "{}".to_string())
    }
}

// ── Decodificador / Ejecutor (privado) ─────────────────────────────────────
impl VirtualCore {
    #[inline(always)]
    fn execute_internal(&mut self, buf: &[u16]) {
        self.halted = false;
        let mut pc: usize = 0;
        let n_neurons = self.neurons.len();
        let n_weights = self.weights.len();

        while pc < buf.len() {
            let inst = buf[pc];
            pc += 1;
            self.cycles_executed = self.cycles_executed.wrapping_add(1);

            // 🚀 Decodificación Bitwise (máxima velocidad en CPU/WASM)
            let opcode  = ((inst >> 11) & 0x1F) as u8;   // bits 15-11
            let target  = ((inst >> 6)  & 0x1F) as usize; // bits 10-6
            let data    = (inst & 0x3F) as u8;            // bits 5-0

            // dirección absoluta = base + target (5 bits → ventana de 32)
            let abs_target = self.base_address.wrapping_add(target);

            // 🚀 Jump Table (Rust compila esto como switch hiper-optimizado)
            match opcode {
                // NOP — 🐚
                0x00 => {}

                // FIRE — forzar spike
                0x01 => {
                    if abs_target < n_neurons {
                        self.neurons[abs_target] = 30.0;
                        self.spike_count = self.spike_count.wrapping_add(1);
                    }
                }

                // INHIB — inhibición (resta data al voltaje)
                0x02 => {
                    if abs_target < n_neurons {
                        self.neurons[abs_target] -= data as f32;
                    }
                }

                // RESET — voltaje a 0
                0x03 => {
                    if abs_target < n_neurons {
                        self.neurons[abs_target] = 0.0;
                    }
                }

                // DECAY — v *= (1 - data/63)
                0x04 => {
                    if abs_target < n_neurons {
                        let factor = 1.0 - (data as f32 / 63.0);
                        self.neurons[abs_target] *= factor.max(0.0);
                    }
                }

                // INTEG — v += data (integración de input)
                0x05 => {
                    if abs_target < n_neurons {
                        self.neurons[abs_target] += data as f32;
                    }
                }

                // THRES — si v > data → spike (v=30) + reset post-spike
                0x06 => {
                    if abs_target < n_neurons {
                        if self.neurons[abs_target] > data as f32 {
                            self.neurons[abs_target] = 30.0;
                            self.spike_count = self.spike_count.wrapping_add(1);
                            // post-spike reset diferido vía siguiente instrucción
                        }
                    }
                }

                // LEAK — v *= 0.95 (leak fijo)
                0x07 => {
                    if abs_target < n_neurons {
                        self.neurons[abs_target] *= 0.95;
                    }
                }

                // WPOT — potenciar peso (clamp 63)
                0x08 => {
                    if abs_target < n_weights {
                        let cur = self.weights[abs_target];
                        self.weights[abs_target] = (cur as u16 + data as u16).min(63) as u8;
                    }
                }

                // WDEP — deprimir peso (clamp 0)
                0x09 => {
                    if abs_target < n_weights {
                        let cur = self.weights[abs_target];
                        self.weights[abs_target] = cur.saturating_sub(data);
                    }
                }

                // WCLR — peso = 0
                0x0A => {
                    if abs_target < n_weights {
                        self.weights[abs_target] = 0;
                    }
                }

                // WSET — peso = data (clamp 63)
                0x0B => {
                    if abs_target < n_weights {
                        self.weights[abs_target] = data.min(63);
                    }
                }

                // STDP — Hebbian: si neurona post (target) hizo spike Y peso pre activo,
                // potencia. Si no hubo spike → deprimir levemente.
                0x0C => {
                    if abs_target < n_neurons && abs_target < n_weights {
                        let post_v = self.neurons[abs_target];
                        let cur_w = self.weights[abs_target];
                        if post_v > 20.0 {
                            // LTP — long-term potentiation
                            let new_w = (cur_w as u16 + data as u16).min(63) as u8;
                            self.weights[abs_target] = new_w;
                        } else {
                            // LTD — long-term depression (mitad de magnitud)
                            let dec = (data >> 1).max(1);
                            self.weights[abs_target] = cur_w.saturating_sub(dec);
                        }
                    }
                }

                // SBASE — rebase ventana: base = target*32 + data
                0x0D => {
                    self.base_address = (target as usize) * 32 + (data as usize);
                }

                // JMP — pc absoluto = target*64 + data
                0x0E => {
                    let new_pc = (target as usize) * 64 + (data as usize);
                    if new_pc < buf.len() {
                        pc = new_pc;
                    } else {
                        // out-of-range jump → halt
                        self.halted = true;
                        break;
                    }
                }

                // JNZ — si v[target] != 0 → pc += data
                0x0F => {
                    if abs_target < n_neurons && self.neurons[abs_target].abs() > 0.001 {
                        pc = pc.saturating_add(data as usize);
                    }
                }

                // BATCH — instrucción extendida (data como sub-opcode)
                0x17 => {
                    match data {
                        0x00 => {
                            // BATCH_LEAK_ALL — leak global
                            for v in self.neurons.iter_mut() { *v *= 0.9; }
                        }
                        0x01 => {
                            // BATCH_RESET_ALL_V — voltajes a 0
                            for v in self.neurons.iter_mut() { *v = 0.0; }
                        }
                        0x02 => {
                            // BATCH_NORM_W — normalizar pesos al rango 6-bit
                            let max_w = *self.weights.iter().max().unwrap_or(&63);
                            if max_w > 0 {
                                let scale = 63.0 / max_w as f32;
                                for w in self.weights.iter_mut() {
                                    *w = ((*w as f32 * scale) as u32).min(63) as u8;
                                }
                            }
                        }
                        0x03 => {
                            // BATCH_THRESHOLD_ALL — todas las neuronas con v>15 → spike
                            for v in self.neurons.iter_mut() {
                                if *v > 15.0 {
                                    *v = 30.0;
                                    self.spike_count = self.spike_count.wrapping_add(1);
                                }
                            }
                        }
                        _ => {} // sub-opcodes reservados
                    }
                }

                // HALT — fin
                0x1F => {
                    self.halted = true;
                    break;
                }

                _ => {} // opcodes reservados (industriales / calidad futura)
            }
        }
    }
}

// ── Helpers públicos a JS para construir instrucciones (encoder de referencia)
#[wasm_bindgen]
pub fn maya_encode(opcode: u8, target: u8, data: u8) -> u16 {
    let op = (opcode as u16 & 0x1F) << 11;
    let tg = (target as u16 & 0x1F) << 6;
    let dt = data as u16 & 0x3F;
    op | tg | dt
}

#[wasm_bindgen]
pub fn maya_decode_opcode(inst: u16) -> u8 { ((inst >> 11) & 0x1F) as u8 }

#[wasm_bindgen]
pub fn maya_decode_target(inst: u16) -> u8 { ((inst >> 6) & 0x1F) as u8 }

#[wasm_bindgen]
pub fn maya_decode_data(inst: u16) -> u8 { (inst & 0x3F) as u8 }

// ── Tests (corren en `cargo test`, no en WASM) ─────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_encode_decode_roundtrip() {
        let inst = maya_encode(0x08, 5, 42);
        assert_eq!(maya_decode_opcode(inst), 0x08);
        assert_eq!(maya_decode_target(inst), 5);
        assert_eq!(maya_decode_data(inst), 42);
    }

    #[test]
    fn test_fire_then_halt() {
        let mut vm = VirtualCore::new(32, 32);
        let prog = vec![
            maya_encode(0x01, 3, 0),    // FIRE neuron 3
            maya_encode(0x01, 7, 0),    // FIRE neuron 7
            maya_encode(0x1F, 0, 0),    // HALT
            maya_encode(0x01, 9, 0),    // (no debe ejecutarse)
        ];
        vm.execute_fast(&prog);
        assert_eq!(vm.spike_count(), 2);
        assert_eq!(vm.get_voltage(3), 30.0);
        assert_eq!(vm.get_voltage(7), 30.0);
        assert_eq!(vm.get_voltage(9), 0.0);
        assert!(vm.is_halted());
    }

    #[test]
    fn test_wpot_clamp_63() {
        let mut vm = VirtualCore::new(32, 32);
        vm.set_weight(0, 60);
        let prog = vec![
            maya_encode(0x08, 0, 10),   // WPOT +10 → debe quedar en 63
            maya_encode(0x1F, 0, 0),
        ];
        vm.execute_fast(&prog);
        assert_eq!(vm.get_weight(0), 63);
    }

    #[test]
    fn test_stdp_ltp_and_ltd() {
        let mut vm = VirtualCore::new(32, 32);
        // LTP: post-neuron con spike alto + STDP → peso sube
        vm.set_voltage(5, 25.0);
        vm.set_weight(5, 10);
        vm.execute_fast(&vec![maya_encode(0x0C, 5, 4), maya_encode(0x1F, 0, 0)]);
        assert_eq!(vm.get_weight(5), 14);

        // LTD: post-neuron silenciosa → peso baja (data/2)
        vm.set_voltage(6, 0.5);
        vm.set_weight(6, 20);
        vm.execute_fast(&vec![maya_encode(0x0C, 6, 8), maya_encode(0x1F, 0, 0)]);
        assert_eq!(vm.get_weight(6), 16);
    }

    #[test]
    fn test_sbase_rebase_window() {
        let mut vm = VirtualCore::new(128, 128);
        let prog = vec![
            maya_encode(0x0D, 2, 0),    // SBASE → base = 2*32 = 64
            maya_encode(0x01, 5, 0),    // FIRE en (64+5) = 69
            maya_encode(0x1F, 0, 0),
        ];
        vm.execute_fast(&prog);
        assert_eq!(vm.get_voltage(69), 30.0);
    }

    #[test]
    fn test_batch_leak_all() {
        let mut vm = VirtualCore::new(32, 32);
        for i in 0..10 { vm.set_voltage(i, 100.0); }
        vm.execute_fast(&vec![maya_encode(0x17, 0, 0x00), maya_encode(0x1F, 0, 0)]);
        for i in 0..10 { assert!((vm.get_voltage(i) - 90.0).abs() < 0.01); }
    }
}
