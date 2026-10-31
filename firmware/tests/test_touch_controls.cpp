#include <cassert>
#include <cstdio>
#include "../src/touch_controls.h"

int main() {
    WatchTouchControls c;
    auto sample = [&](bool down,int x,int y) { return c.update(1,down,x,y); };
    assert(sample(true,170,400).consumed);
    assert(sample(true,170,400).volumeDelta == 0);
    assert(sample(false,170,400).callButton == 1);
    assert(sample(false,170,400).callButton == 0); // Same frame processed twice.
    assert(sample(true,300,400).consumed);
    assert(sample(false,300,400).callButton == 2);
    sample(true,170,400); sample(true,170,350); // Drag out and back cancels ON.
    assert(sample(false,170,400).callButton == 0);

    sample(true,230,240);
    assert(sample(true,233,229).volumeDelta == 0); // Ignore small finger jitter.
    auto up = sample(true,230,204);
    assert(up.consumed && up.volumeDelta == 26 && !up.callButton);
    assert(sample(true,230,204).volumeDelta == 0); // No repeat at fixed position.
    assert(sample(true,230,222).volumeDelta == -13); // Reverse while touching.
    assert(sample(false,230,222).callButton == 0);
    sample(true,300,280);
    assert(sample(true,300,316).volumeDelta == -26);
    assert(sample(false,300,400).callButton == 0); // Swipe ends over OFF.
    sample(true,230,240);
    assert(sample(true,290,250).volumeDelta == 0); // Horizontal drag is not volume.
    assert(sample(false,290,250).callButton == 0);

    sample(true,170,400);
    assert(c.update(2,true,170,400).consumed); // Multi-touch cancels call gesture.
    assert(sample(false,170,400).callButton == 0);
    c.update(0,false,0,0);
    sample(true,170,400);
    c.update(0,false,0,0); // Lost contact must never activate a button.
    assert(sample(false,170,400).callButton == 0);
    sample(true,300,71); // Battery/status area remains non-interactive.
    assert(sample(false,300,200).volumeDelta == 0);
    std::puts("touch: up/down/reversal, repeat suppression, tap and drag isolation, multi-touch/lost contact passed");
}
