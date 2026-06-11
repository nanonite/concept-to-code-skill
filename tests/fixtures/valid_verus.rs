use vstd::prelude::*;

verus! {
pub fn identity(x: usize) -> (result: usize)
    requires
        x < 10,
    ensures
        result == x,
{
    x
}
}
