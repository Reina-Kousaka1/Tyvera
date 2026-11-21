//! Precise, non-moving, generational heap prototype.
//!
//! Object fields are represented as typed [`Value`]s, so collection follows
//! references directly instead of scanning arbitrary machine words. The
//! compiler and native IR do not yet allocate these objects.

use std::collections::HashSet;
use std::mem::size_of;

const PROMOTION_AGE: u8 = 2;

/// A slot handle whose generation changes whenever its slot is reused.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ObjectRef {
    slot: u32,
    generation: u32,
}

/// Runtime values visible to the collector.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Nil,
    Unit,
    Bool(bool),
    Int(i64),
    Float(f64),
    Ref(ObjectRef),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Generation {
    Young,
    Old,
}

struct HeapObject {
    fields: Vec<Value>,
    generation: Generation,
    age: u8,
}

struct Slot {
    generation: u32,
    object: Option<Box<HeapObject>>,
}

/// Which generations a collection cycle may reclaim.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CollectionKind {
    /// Reclaim young objects and promote survivors after enough cycles.
    Minor,
    /// Trace and reclaim objects in every generation.
    Full,
}

/// Counts from one collection cycle.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct CollectionStats {
    pub visited: usize,
    pub reclaimed: usize,
    pub promoted: usize,
    pub bytes_reclaimed: usize,
}

/// Invalid or stale object handles and out-of-range field accesses.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HeapError {
    InvalidReference,
    FieldOutOfBounds,
    TooManyObjects,
}

impl std::fmt::Display for HeapError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidReference => write!(f, "invalid or stale heap reference"),
            Self::FieldOutOfBounds => write!(f, "heap field index is out of bounds"),
            Self::TooManyObjects => write!(f, "heap object handle space is exhausted"),
        }
    }
}

impl std::error::Error for HeapError {}

/// A single-threaded heap. Each live object's allocation remains at a stable
/// address until that object is reclaimed.
#[derive(Default)]
pub struct Heap {
    slots: Vec<Slot>,
    free: Vec<u32>,
    remembered: HashSet<ObjectRef>,
    allocated_bytes: usize,
}

impl Heap {
    pub fn new() -> Self {
        Self::default()
    }

    /// Allocate an object with `field_count` nil-initialized fields.
    pub fn allocate(&mut self, field_count: usize) -> Result<ObjectRef, HeapError> {
        if self.free.is_empty() && u32::try_from(self.slots.len()).is_err() {
            return Err(HeapError::TooManyObjects);
        }
        let object = Box::new(HeapObject {
            fields: vec![Value::Nil; field_count],
            generation: Generation::Young,
            age: 0,
        });
        self.allocated_bytes += object_size(&object);

        if let Some(slot_index) = self.free.pop() {
            let slot = &mut self.slots[slot_index as usize];
            debug_assert!(slot.object.is_none());
            slot.object = Some(object);
            return Ok(ObjectRef {
                slot: slot_index,
                generation: slot.generation,
            });
        }

        let slot_index = self.slots.len() as u32;
        self.slots.push(Slot {
            generation: 0,
            object: Some(object),
        });
        Ok(ObjectRef {
            slot: slot_index,
            generation: 0,
        })
    }

    /// Read one field, copying its scalar or reference value.
    pub fn get(&self, object: ObjectRef, field: usize) -> Result<Value, HeapError> {
        self.object(object)?
            .fields
            .get(field)
            .cloned()
            .ok_or(HeapError::FieldOutOfBounds)
    }

    /// Store one field and record old-to-young edges for minor collections.
    pub fn set(&mut self, object: ObjectRef, field: usize, value: Value) -> Result<(), HeapError> {
        if let Value::Ref(reference) = value {
            self.object(reference)?;
            let target_is_young = self.object(reference)?.generation == Generation::Young;
            let owner = self.object_mut(object)?;
            let slot = owner
                .fields
                .get_mut(field)
                .ok_or(HeapError::FieldOutOfBounds)?;
            *slot = Value::Ref(reference);
            if owner.generation == Generation::Old && target_is_young {
                self.remembered.insert(object);
            }
            return Ok(());
        }

        *self
            .object_mut(object)?
            .fields
            .get_mut(field)
            .ok_or(HeapError::FieldOutOfBounds)? = value;
        Ok(())
    }

    /// Number of bytes currently reserved for live object headers and fields.
    pub fn allocated_bytes(&self) -> usize {
        self.allocated_bytes
    }

    /// Run a precise collection using the supplied stack/global roots.
    pub fn collect(
        &mut self,
        roots: &[Value],
        kind: CollectionKind,
    ) -> Result<CollectionStats, HeapError> {
        let mut marked = HashSet::new();
        let mut work = Vec::new();
        for root in roots {
            if let Value::Ref(reference) = root {
                self.object(*reference)?;
                work.push(*reference);
            }
        }

        // Minor GC also traces every remembered old object. This is safe even
        // if some remembered owners are otherwise dead: old objects survive a
        // minor cycle, and the next full cycle reclaims the entire dead graph.
        if kind == CollectionKind::Minor {
            work.extend(self.remembered.iter().copied());
        }

        while let Some(reference) = work.pop() {
            if !marked.insert(reference) {
                continue;
            }
            let object = self.object(reference)?;
            for field in &object.fields {
                if let Value::Ref(child) = field {
                    work.push(*child);
                }
            }
        }

        let mut stats = CollectionStats {
            visited: marked.len(),
            ..CollectionStats::default()
        };
        for index in 0..self.slots.len() {
            let Some(object) = self.slots[index].object.as_ref() else {
                continue;
            };
            let reference = ObjectRef {
                slot: index as u32,
                generation: self.slots[index].generation,
            };
            let eligible = kind == CollectionKind::Full || object.generation == Generation::Young;
            if eligible && !marked.contains(&reference) {
                let object = self.slots[index].object.take().unwrap();
                let bytes = object_size(&object);
                self.allocated_bytes -= bytes;
                stats.reclaimed += 1;
                stats.bytes_reclaimed += bytes;
                self.slots[index].generation = self.slots[index].generation.wrapping_add(1);
                self.free.push(index as u32);
                self.remembered.remove(&reference);
            } else if object.generation == Generation::Young && marked.contains(&reference) {
                let object = self.slots[index].object.as_mut().unwrap();
                object.age = object.age.saturating_add(1);
                if object.age >= PROMOTION_AGE {
                    object.generation = Generation::Old;
                    stats.promoted += 1;
                }
            }
        }
        self.remembered = self
            .remembered
            .iter()
            .copied()
            .filter(|reference| {
                let Some(object) = self
                    .slots
                    .get(reference.slot as usize)
                    .filter(|slot| slot.generation == reference.generation)
                    .and_then(|slot| slot.object.as_ref())
                else {
                    return false;
                };
                object.generation == Generation::Old
                    && object.fields.iter().any(|value| match value {
                        Value::Ref(child) => self
                            .slots
                            .get(child.slot as usize)
                            .filter(|slot| slot.generation == child.generation)
                            .and_then(|slot| slot.object.as_ref())
                            .is_some_and(|object| object.generation == Generation::Young),
                        _ => false,
                    })
            })
            .collect();
        Ok(stats)
    }

    fn object(&self, reference: ObjectRef) -> Result<&HeapObject, HeapError> {
        self.slots
            .get(reference.slot as usize)
            .filter(|slot| slot.generation == reference.generation)
            .and_then(|slot| slot.object.as_deref())
            .ok_or(HeapError::InvalidReference)
    }

    fn object_mut(&mut self, reference: ObjectRef) -> Result<&mut HeapObject, HeapError> {
        self.slots
            .get_mut(reference.slot as usize)
            .filter(|slot| slot.generation == reference.generation)
            .and_then(|slot| slot.object.as_deref_mut())
            .ok_or(HeapError::InvalidReference)
    }

    #[cfg(test)]
    fn address(&self, reference: ObjectRef) -> Result<usize, HeapError> {
        Ok(self.object(reference)? as *const HeapObject as usize)
    }
}

fn object_size(object: &HeapObject) -> usize {
    size_of::<HeapObject>() + object.fields.capacity() * size_of::<Value>()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_collection_keeps_only_the_precisely_reachable_graph() {
        let mut heap = Heap::new();
        let root = heap.allocate(1).unwrap();
        let child = heap.allocate(0).unwrap();
        let garbage = heap.allocate(0).unwrap();
        heap.set(root, 0, Value::Ref(child)).unwrap();
        let before = heap.allocated_bytes();

        let stats = heap
            .collect(&[Value::Ref(root)], CollectionKind::Full)
            .unwrap();

        assert_eq!(stats.reclaimed, 1);
        assert!(stats.bytes_reclaimed > 0);
        assert_eq!(heap.allocated_bytes(), before - stats.bytes_reclaimed);
        assert_eq!(heap.get(root, 0).unwrap(), Value::Ref(child));
        assert_eq!(heap.get(garbage, 0), Err(HeapError::InvalidReference));
    }

    #[test]
    fn minor_collection_promotes_survivors_without_moving_them() {
        let mut heap = Heap::new();
        let root = heap.allocate(0).unwrap();
        let address = heap.address(root).unwrap();

        assert_eq!(
            heap.collect(&[Value::Ref(root)], CollectionKind::Minor)
                .unwrap()
                .promoted,
            0
        );
        assert_eq!(
            heap.collect(&[Value::Ref(root)], CollectionKind::Minor)
                .unwrap()
                .promoted,
            1
        );
        assert_eq!(heap.address(root).unwrap(), address);
    }

    #[test]
    fn write_barrier_preserves_young_objects_referenced_by_old_objects() {
        let mut heap = Heap::new();
        let old = heap.allocate(1).unwrap();
        heap.collect(&[Value::Ref(old)], CollectionKind::Minor)
            .unwrap();
        heap.collect(&[Value::Ref(old)], CollectionKind::Minor)
            .unwrap();
        let young = heap.allocate(0).unwrap();
        heap.set(old, 0, Value::Ref(young)).unwrap();

        let stats = heap.collect(&[], CollectionKind::Minor).unwrap();

        assert_eq!(stats.reclaimed, 0);
        assert_eq!(heap.get(old, 0).unwrap(), Value::Ref(young));
    }

    #[test]
    fn reused_slots_reject_stale_handles() {
        let mut heap = Heap::new();
        let stale = heap.allocate(0).unwrap();
        heap.collect(&[], CollectionKind::Full).unwrap();
        let current = heap.allocate(0).unwrap();

        assert_eq!(stale.slot, current.slot);
        assert_ne!(stale.generation, current.generation);
        assert_eq!(heap.get(stale, 0), Err(HeapError::InvalidReference));
    }
}
