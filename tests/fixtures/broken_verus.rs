use vstd::prelude::*;

verus! {
pub fn identity(x: usize) -> (result: usize)
    requires
        x == true,
    ensures
        result == x,
{
    x
}
}
