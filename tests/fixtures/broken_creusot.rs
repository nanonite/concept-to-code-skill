#![allow(unexpected_cfgs)]

pub fn plain_runtime_predicate(_: u32) -> bool {
    true
}

#[cfg_attr(creusot, contracts::creusot::ensures(plain_runtime_predicate(result)))]
pub fn identity(x: u32) -> u32 {
    x
}
