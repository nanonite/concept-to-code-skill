#![allow(unexpected_cfgs)]

#[cfg_attr(kani, kani::requires(nonexistent_precondition(x)))]
pub fn increment_bounded(x: u32) -> u32 {
    x + 1
}

#[cfg(kani)]
#[kani::proof]
fn broken_harness() {
    let _ = increment_bounded(1);
}
