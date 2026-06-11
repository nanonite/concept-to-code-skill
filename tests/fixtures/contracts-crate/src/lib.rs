//! Minimal verifier-contracts facade fixture for the gate self-tests.
//!
//! Mirrors the shape a real `contracts` crate must expose for
//! `emit_stubs.py`'s default `--contracts-crate contracts`: top-level
//! `creusot` and `verus` modules, with `creusot::prelude` and
//! `verus::prelude` re-exports plus the `creusot_f64_*` model-predicate
//! helpers referenced by translated `logic` expressions.
//!
//! At runtime / under verification, exactly one of the three Cargo features
//! (`kani`, `creusot`, `verus`) is active, which decides which verifier
//! crate is actually pulled in. The default (no feature) produces empty
//! modules so a generated stub still compiles without any verifier
//! installed.

/// Kani contract and proof-harness helpers.
#[cfg(feature = "kani")]
pub mod kani {
    pub use ::kani::*;
}

/// Placeholder Kani namespace for non-Kani builds.
#[cfg(not(feature = "kani"))]
pub mod kani {}

/// Creusot contracts and prelude for normal Cargo macro-syntax builds.
#[cfg(all(feature = "creusot", not(creusot)))]
pub mod creusot {
    pub use ::creusot_contracts::*;

    // Wildcard `pub use ::creusot_contracts::*` re-exports value items but
    // not attribute proc-macros under their original names — proc-macros
    // need explicit re-export to be reachable via the facade path
    // `contracts::creusot::<name>`, which is what emit_stubs.py emits.
    pub use ::creusot_contracts::macros::{
        ensures, invariant, logic, requires, trusted,
    };

    /// Trusted f64 model predicates used only inside generated Creusot attributes.
    pub fn creusot_f64_lt(left: f64, right: f64) -> bool { left < right }
    pub fn creusot_f64_le(left: f64, right: f64) -> bool { left <= right }
    pub fn creusot_f64_gt(left: f64, right: f64) -> bool { left > right }
    pub fn creusot_f64_ge(left: f64, right: f64) -> bool { left >= right }
    pub fn creusot_f64_eq(left: f64, right: f64) -> bool { left == right }
    pub fn creusot_f64_lt_zero(value: f64) -> bool { value < 0.0 }
    pub fn creusot_f64_le_zero(value: f64) -> bool { value <= 0.0 }
    pub fn creusot_f64_gt_zero(value: f64) -> bool { value > 0.0 }
    pub fn creusot_f64_ge_zero(value: f64) -> bool { value >= 0.0 }
    pub fn creusot_f64_eq_zero(value: f64) -> bool { value == 0.0 }
    pub fn creusot_f64_is_finite(value: f64) -> bool { value.is_finite() }
    pub fn creusot_f64_is_nan(value: f64) -> bool { value.is_nan() }

    /// Creusot prelude re-export used by generated stubs.
    pub mod prelude {
        pub use ::creusot_contracts::prelude::*;
    }
}

/// Creusot contracts and prelude for the cargo-creusot driver pass.
#[cfg(all(feature = "creusot", creusot))]
pub mod creusot {
    pub use ::creusot_std::*;
    pub use ::creusot_std::macros::{ensures, invariant, logic, requires, trusted};

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_lt(_left: f64, _right: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_le(_left: f64, _right: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_gt(_left: f64, _right: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_ge(_left: f64, _right: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_eq(_left: f64, _right: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_lt_zero(_value: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_le_zero(_value: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_gt_zero(_value: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_ge_zero(_value: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_eq_zero(_value: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_is_finite(_value: f64) -> bool { pearlite! { true } }

    #[trusted]
    #[logic(opaque)]
    pub fn creusot_f64_is_nan(_value: f64) -> bool { pearlite! { true } }

    /// Creusot prelude re-export used by generated stubs.
    pub mod prelude {
        pub use ::creusot_std::prelude::*;
    }
}

/// Placeholder Creusot namespace for non-Creusot builds.
#[cfg(not(feature = "creusot"))]
pub mod creusot {}

/// Verus standard-library proof helpers.
#[cfg(feature = "verus")]
pub mod verus {
    pub use ::vstd::*;

    /// Verus prelude re-export used by generated stubs.
    pub mod prelude {
        pub use ::vstd::prelude::*;
    }
}

/// Placeholder Verus namespace for non-Verus builds.
#[cfg(not(feature = "verus"))]
pub mod verus {}
