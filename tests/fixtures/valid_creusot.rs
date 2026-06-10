#![allow(unexpected_cfgs)]

#[cfg_attr(creusot, contracts::creusot::ensures(result@ == x@))]
pub fn identity(x: u32) -> u32 {
    x
}
