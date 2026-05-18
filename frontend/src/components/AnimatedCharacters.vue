<script setup lang="ts">
import {
  computed,
  defineComponent,
  h,
  onMounted,
  onUnmounted,
  ref,
  watch,
  type CSSProperties,
  type PropType,
} from "vue";

const props = withDefaults(
  defineProps<{
    isTyping?: boolean;
    showPassword?: boolean;
    passwordLength?: number;
  }>(),
  {
    isTyping: false,
    showPassword: false,
    passwordLength: 0,
  },
);

const Pupil = defineComponent({
  name: "Pupil",
  props: {
    size: { type: Number, default: 12 },
    maxDistance: { type: Number, default: 5 },
    pupilColor: { type: String, default: "black" },
    forceLookX: { type: Number as PropType<number | undefined>, default: undefined },
    forceLookY: { type: Number as PropType<number | undefined>, default: undefined },
  },
  setup(componentProps) {
    const mouseX = ref(0);
    const mouseY = ref(0);
    const pupilRef = ref<HTMLDivElement | null>(null);

    const handleMouseMove = (event: MouseEvent) => {
      mouseX.value = event.clientX;
      mouseY.value = event.clientY;
    };

    onMounted(() => {
      window.addEventListener("mousemove", handleMouseMove);
    });

    onUnmounted(() => {
      window.removeEventListener("mousemove", handleMouseMove);
    });

    const pupilPosition = computed(() => {
      if (!pupilRef.value) return { x: 0, y: 0 };

      if (
        componentProps.forceLookX !== undefined &&
        componentProps.forceLookY !== undefined
      ) {
        return { x: componentProps.forceLookX, y: componentProps.forceLookY };
      }

      const pupil = pupilRef.value.getBoundingClientRect();
      const pupilCenterX = pupil.left + pupil.width / 2;
      const pupilCenterY = pupil.top + pupil.height / 2;

      const deltaX = mouseX.value - pupilCenterX;
      const deltaY = mouseY.value - pupilCenterY;
      const distance = Math.min(
        Math.sqrt(deltaX ** 2 + deltaY ** 2),
        componentProps.maxDistance,
      );

      const angle = Math.atan2(deltaY, deltaX);

      return {
        x: Math.cos(angle) * distance,
        y: Math.sin(angle) * distance,
      };
    });

    return () =>
      h("div", {
        ref: pupilRef,
        style: {
          width: `${componentProps.size}px`,
          height: `${componentProps.size}px`,
          borderRadius: "999px",
          backgroundColor: componentProps.pupilColor,
          transform: `translate(${pupilPosition.value.x}px, ${pupilPosition.value.y}px)`,
          transition: "transform 0.1s ease-out",
        } satisfies CSSProperties,
      });
  },
});

const EyeBall = defineComponent({
  name: "EyeBall",
  props: {
    size: { type: Number, default: 48 },
    pupilSize: { type: Number, default: 16 },
    maxDistance: { type: Number, default: 10 },
    eyeColor: { type: String, default: "white" },
    pupilColor: { type: String, default: "black" },
    isBlinking: { type: Boolean, default: false },
    forceLookX: { type: Number as PropType<number | undefined>, default: undefined },
    forceLookY: { type: Number as PropType<number | undefined>, default: undefined },
  },
  setup(componentProps) {
    const mouseX = ref(0);
    const mouseY = ref(0);
    const eyeRef = ref<HTMLDivElement | null>(null);

    const handleMouseMove = (event: MouseEvent) => {
      mouseX.value = event.clientX;
      mouseY.value = event.clientY;
    };

    onMounted(() => {
      window.addEventListener("mousemove", handleMouseMove);
    });

    onUnmounted(() => {
      window.removeEventListener("mousemove", handleMouseMove);
    });

    const pupilPosition = computed(() => {
      if (!eyeRef.value) return { x: 0, y: 0 };

      if (
        componentProps.forceLookX !== undefined &&
        componentProps.forceLookY !== undefined
      ) {
        return { x: componentProps.forceLookX, y: componentProps.forceLookY };
      }

      const eye = eyeRef.value.getBoundingClientRect();
      const eyeCenterX = eye.left + eye.width / 2;
      const eyeCenterY = eye.top + eye.height / 2;

      const deltaX = mouseX.value - eyeCenterX;
      const deltaY = mouseY.value - eyeCenterY;
      const distance = Math.min(
        Math.sqrt(deltaX ** 2 + deltaY ** 2),
        componentProps.maxDistance,
      );

      const angle = Math.atan2(deltaY, deltaX);

      return {
        x: Math.cos(angle) * distance,
        y: Math.sin(angle) * distance,
      };
    });

    return () =>
      h(
        "div",
        {
          ref: eyeRef,
          style: {
            width: `${componentProps.size}px`,
            height: componentProps.isBlinking ? "2px" : `${componentProps.size}px`,
            borderRadius: "999px",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            backgroundColor: componentProps.eyeColor,
            overflow: "hidden",
            transition: "all 150ms ease",
          } satisfies CSSProperties,
        },
        componentProps.isBlinking
          ? []
          : [
              h("div", {
                style: {
                  width: `${componentProps.pupilSize}px`,
                  height: `${componentProps.pupilSize}px`,
                  borderRadius: "999px",
                  backgroundColor: componentProps.pupilColor,
                  transform: `translate(${pupilPosition.value.x}px, ${pupilPosition.value.y}px)`,
                  transition: "transform 0.1s ease-out",
                } satisfies CSSProperties,
              }),
            ],
      );
  },
});

const mouseX = ref(0);
const mouseY = ref(0);
const isPurpleBlinking = ref(false);
const isBlackBlinking = ref(false);
const isLookingAtEachOther = ref(false);
const isPurplePeeking = ref(false);
const purpleRef = ref<HTMLDivElement | null>(null);
const blackRef = ref<HTMLDivElement | null>(null);
const yellowRef = ref<HTMLDivElement | null>(null);
const orangeRef = ref<HTMLDivElement | null>(null);

let purpleBlinkTimer: ReturnType<typeof setTimeout> | undefined;
let purpleBlinkResetTimer: ReturnType<typeof setTimeout> | undefined;
let blackBlinkTimer: ReturnType<typeof setTimeout> | undefined;
let blackBlinkResetTimer: ReturnType<typeof setTimeout> | undefined;
let lookTimer: ReturnType<typeof setTimeout> | undefined;
let peekTimer: ReturnType<typeof setTimeout> | undefined;
let peekResetTimer: ReturnType<typeof setTimeout> | undefined;

const handleMouseMove = (event: MouseEvent) => {
  mouseX.value = event.clientX;
  mouseY.value = event.clientY;
};

const clearTimer = (timer: ReturnType<typeof setTimeout> | undefined) => {
  if (timer) clearTimeout(timer);
};

const schedulePurpleBlink = () => {
  purpleBlinkTimer = setTimeout(
    () => {
      isPurpleBlinking.value = true;
      purpleBlinkResetTimer = setTimeout(() => {
        isPurpleBlinking.value = false;
        schedulePurpleBlink();
      }, 150);
    },
    Math.random() * 4000 + 3000,
  );
};

const scheduleBlackBlink = () => {
  blackBlinkTimer = setTimeout(
    () => {
      isBlackBlinking.value = true;
      blackBlinkResetTimer = setTimeout(() => {
        isBlackBlinking.value = false;
        scheduleBlackBlink();
      }, 150);
    },
    Math.random() * 4000 + 3000,
  );
};

const schedulePeek = () => {
  clearTimer(peekTimer);
  clearTimer(peekResetTimer);

  if (props.passwordLength > 0 && props.showPassword) {
    peekTimer = setTimeout(
      () => {
        isPurplePeeking.value = true;
        peekResetTimer = setTimeout(() => {
          isPurplePeeking.value = false;
          schedulePeek();
        }, 800);
      },
      Math.random() * 3000 + 2000,
    );
  } else {
    isPurplePeeking.value = false;
  }
};

const calculatePosition = (targetRef: typeof purpleRef) => {
  if (!targetRef.value) return { faceX: 0, faceY: 0, bodySkew: 0 };

  const rect = targetRef.value.getBoundingClientRect();
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 3;

  const deltaX = mouseX.value - centerX;
  const deltaY = mouseY.value - centerY;

  const faceX = Math.max(-22, Math.min(22, deltaX / 16));
  const faceY = Math.max(-16, Math.min(16, deltaY / 22));
  const bodySkew = Math.max(-10, Math.min(10, -deltaX / 85));

  return { faceX, faceY, bodySkew };
};

const purplePos = computed(() => calculatePosition(purpleRef));
const blackPos = computed(() => calculatePosition(blackRef));
const yellowPos = computed(() => calculatePosition(yellowRef));
const orangePos = computed(() => calculatePosition(orangeRef));
const isHidingPassword = computed(() => props.passwordLength > 0 && !props.showPassword);
const isShowingPassword = computed(() => props.passwordLength > 0 && props.showPassword);

watch(
  () => props.isTyping,
  (isTyping) => {
    clearTimer(lookTimer);

    if (isTyping) {
      isLookingAtEachOther.value = true;
      lookTimer = setTimeout(() => {
        isLookingAtEachOther.value = false;
      }, 800);
    } else {
      isLookingAtEachOther.value = false;
    }
  },
);

watch(
  () => [props.passwordLength, props.showPassword],
  () => {
    schedulePeek();
  },
);

onMounted(() => {
  window.addEventListener("mousemove", handleMouseMove);
  schedulePurpleBlink();
  scheduleBlackBlink();
  schedulePeek();
});

onUnmounted(() => {
  window.removeEventListener("mousemove", handleMouseMove);
  [
    purpleBlinkTimer,
    purpleBlinkResetTimer,
    blackBlinkTimer,
    blackBlinkResetTimer,
    lookTimer,
    peekTimer,
    peekResetTimer,
  ].forEach(clearTimer);
});
</script>

<template>
  <div class="monster-stage">
    <div
      ref="purpleRef"
      class="monster-body monster-body-slow monster-drop monster-drop-purple"
      :style="{
        left: '70px',
        width: '180px',
        height: props.isTyping || isHidingPassword ? '440px' : '400px',
        backgroundColor: '#6C3FF5',
        borderRadius: '10px 10px 0 0',
        '--monster-drop-bottom-radius': '52px',
        '--monster-settle-bottom-radius': '20px',
        '--monster-rest-bottom-radius': '0',
        zIndex: 1,
        transform: isShowingPassword
          ? 'skewX(0deg)'
          : props.isTyping || isHidingPassword
            ? `skewX(${purplePos.bodySkew - 12}deg) translateX(40px)`
            : `skewX(${purplePos.bodySkew}deg)`,
        transformOrigin: 'bottom center',
      }"
    >
      <div
        class="monster-eyes monster-eyes-purple monster-eyes-slow"
        :style="{
          left: isShowingPassword
            ? '20px'
            : isLookingAtEachOther
              ? '55px'
              : `${45 + purplePos.faceX}px`,
          top: isShowingPassword
            ? '35px'
            : isLookingAtEachOther
              ? '65px'
              : `${40 + purplePos.faceY}px`,
        }"
      >
        <EyeBall
          :size="18"
          :pupil-size="7"
          :max-distance="7"
          eye-color="white"
          pupil-color="#2D2D2D"
          :is-blinking="isPurpleBlinking"
          :force-look-x="isShowingPassword ? (isPurplePeeking ? 4 : -4) : isLookingAtEachOther ? 3 : undefined"
          :force-look-y="isShowingPassword ? (isPurplePeeking ? 5 : -4) : isLookingAtEachOther ? 4 : undefined"
        />
        <EyeBall
          :size="18"
          :pupil-size="7"
          :max-distance="7"
          eye-color="white"
          pupil-color="#2D2D2D"
          :is-blinking="isPurpleBlinking"
          :force-look-x="isShowingPassword ? (isPurplePeeking ? 4 : -4) : isLookingAtEachOther ? 3 : undefined"
          :force-look-y="isShowingPassword ? (isPurplePeeking ? 5 : -4) : isLookingAtEachOther ? 4 : undefined"
        />
      </div>
    </div>

    <div
      ref="blackRef"
      class="monster-body monster-body-slow monster-drop monster-drop-black"
      :style="{
        left: '240px',
        width: '120px',
        height: '310px',
        backgroundColor: '#2D2D2D',
        borderRadius: '8px 8px 0 0',
        '--monster-drop-bottom-radius': '42px',
        '--monster-settle-bottom-radius': '16px',
        '--monster-rest-bottom-radius': '0',
        zIndex: 2,
        transform: isShowingPassword
          ? 'skewX(0deg)'
          : isLookingAtEachOther
            ? `skewX(${blackPos.bodySkew * 1.5 + 10}deg) translateX(20px)`
            : props.isTyping || isHidingPassword
              ? `skewX(${blackPos.bodySkew * 1.5}deg)`
              : `skewX(${blackPos.bodySkew}deg)`,
        transformOrigin: 'bottom center',
      }"
    >
      <div
        class="monster-eyes monster-eyes-black monster-eyes-slow"
        :style="{
          left: isShowingPassword
            ? '10px'
            : isLookingAtEachOther
              ? '32px'
              : `${26 + blackPos.faceX}px`,
          top: isShowingPassword
            ? '28px'
            : isLookingAtEachOther
              ? '12px'
              : `${32 + blackPos.faceY}px`,
        }"
      >
        <EyeBall
          :size="16"
          :pupil-size="6"
          :max-distance="6"
          eye-color="white"
          pupil-color="#2D2D2D"
          :is-blinking="isBlackBlinking"
          :force-look-x="isShowingPassword ? -4 : isLookingAtEachOther ? 0 : undefined"
          :force-look-y="isShowingPassword ? -4 : isLookingAtEachOther ? -4 : undefined"
        />
        <EyeBall
          :size="16"
          :pupil-size="6"
          :max-distance="6"
          eye-color="white"
          pupil-color="#2D2D2D"
          :is-blinking="isBlackBlinking"
          :force-look-x="isShowingPassword ? -4 : isLookingAtEachOther ? 0 : undefined"
          :force-look-y="isShowingPassword ? -4 : isLookingAtEachOther ? -4 : undefined"
        />
      </div>
    </div>

    <div
      ref="orangeRef"
      class="monster-body monster-body-slow monster-drop monster-drop-orange"
      :style="{
        left: '0px',
        width: '280px',
        height: '140px',
        zIndex: 3,
        backgroundColor: '#FF9B6B',
        borderRadius: '140px 140px 0 0',
        '--monster-drop-bottom-radius': '68px',
        '--monster-settle-bottom-radius': '26px',
        '--monster-rest-bottom-radius': '0',
        transform: isShowingPassword ? 'skewX(0deg)' : `skewX(${orangePos.bodySkew}deg)`,
        transformOrigin: 'bottom center',
      }"
    >
      <div
        class="monster-eyes monster-eyes-orange monster-eyes-fast"
        :style="{
          left: isShowingPassword ? '70px' : `${102 + orangePos.faceX}px`,
          top: isShowingPassword ? '52px' : `${58 + orangePos.faceY}px`,
        }"
      >
        <Pupil
          :size="12"
          :max-distance="7"
          pupil-color="#2D2D2D"
          :force-look-x="isShowingPassword ? -5 : undefined"
          :force-look-y="isShowingPassword ? -4 : undefined"
        />
        <Pupil
          :size="12"
          :max-distance="7"
          pupil-color="#2D2D2D"
          :force-look-x="isShowingPassword ? -5 : undefined"
          :force-look-y="isShowingPassword ? -4 : undefined"
        />
      </div>

      <div
        class="monster-mouth monster-mouth-orange"
        :style="{
          left: isShowingPassword ? '82px' : `${114 + orangePos.faceX}px`,
          top: isShowingPassword ? '88px' : `${94 + orangePos.faceY}px`,
        }"
      />
    </div>

    <div
      ref="yellowRef"
      class="monster-body monster-body-slow monster-drop monster-drop-yellow"
      :style="{
        left: '310px',
        width: '140px',
        height: '230px',
        backgroundColor: '#E8D754',
        borderRadius: '70px 70px 0 0',
        '--monster-drop-bottom-radius': '48px',
        '--monster-settle-bottom-radius': '18px',
        '--monster-rest-bottom-radius': '0',
        zIndex: 4,
        transform: isShowingPassword ? 'skewX(0deg)' : `skewX(${yellowPos.bodySkew}deg)`,
        transformOrigin: 'bottom center',
      }"
    >
      <div
        class="monster-eyes monster-eyes-yellow monster-eyes-fast"
        :style="{
          left: isShowingPassword ? '20px' : `${52 + yellowPos.faceX}px`,
          top: isShowingPassword ? '35px' : `${40 + yellowPos.faceY}px`,
        }"
      >
        <Pupil
          :size="12"
          :max-distance="7"
          pupil-color="#2D2D2D"
          :force-look-x="isShowingPassword ? -5 : undefined"
          :force-look-y="isShowingPassword ? -4 : undefined"
        />
        <Pupil
          :size="12"
          :max-distance="7"
          pupil-color="#2D2D2D"
          :force-look-x="isShowingPassword ? -5 : undefined"
          :force-look-y="isShowingPassword ? -4 : undefined"
        />
      </div>

      <div
        class="monster-mouth monster-mouth-yellow"
        :style="{
          left: isShowingPassword ? '10px' : `${40 + yellowPos.faceX}px`,
          top: isShowingPassword ? '88px' : `${88 + yellowPos.faceY}px`,
        }"
      />
    </div>
  </div>
</template>

<style scoped>
.monster-stage {
  position: relative;
  width: 550px;
  height: 400px;
}

.monster-body {
  position: absolute;
  bottom: 0;
}

.monster-drop {
  animation: monster-drop-in 1680ms linear backwards;
  transform-origin: bottom center;
  will-change: translate, scale, opacity, border-bottom-left-radius, border-bottom-right-radius;
}

.monster-drop-purple {
  animation-delay: 80ms;
}

.monster-drop-black {
  animation-delay: 420ms;
}

.monster-drop-orange {
  animation-delay: 760ms;
}

.monster-drop-yellow {
  animation-delay: 1100ms;
}

.monster-body-slow {
  transition: all 700ms ease-in-out;
}

.monster-eyes {
  position: absolute;
  display: flex;
}

.monster-eyes-purple,
.monster-eyes-orange {
  gap: 32px;
}

.monster-eyes-black,
.monster-eyes-yellow {
  gap: 24px;
}

.monster-eyes-slow {
  transition: all 700ms ease-in-out;
}

.monster-eyes-fast {
  transition: all 200ms ease-out;
}

.monster-mouth {
  position: absolute;
  width: 28px;
  height: 8px;
  background: #2D2D2D;
  border-radius: 999px;
  transition: all 200ms ease-out;
}

.monster-mouth-orange {
  width: 16px;
  height: 16px;
}

.monster-mouth-yellow {
  width: 30px;
  height: 6px;
}

@keyframes monster-drop-in {
  0% {
    opacity: 0;
    translate: 0 -660px;
    scale: 0.94 1.06;
    border-bottom-left-radius: var(--monster-drop-bottom-radius);
    border-bottom-right-radius: var(--monster-drop-bottom-radius);
    animation-timing-function: cubic-bezier(0.58, 0.02, 0.92, 0.48);
  }

  42% {
    opacity: 1;
    translate: 0 0;
    scale: 1.1 0.86;
    border-bottom-left-radius: var(--monster-drop-bottom-radius);
    border-bottom-right-radius: var(--monster-drop-bottom-radius);
    animation-timing-function: cubic-bezier(0.18, 0.78, 0.24, 1);
  }

  58% {
    translate: 0 -72px;
    scale: 0.96 1.05;
    border-bottom-left-radius: var(--monster-drop-bottom-radius);
    border-bottom-right-radius: var(--monster-drop-bottom-radius);
    animation-timing-function: cubic-bezier(0.42, 0, 0.88, 0.56);
  }

  72% {
    translate: 0 0;
    scale: 1.06 0.92;
    border-bottom-left-radius: var(--monster-drop-bottom-radius);
    border-bottom-right-radius: var(--monster-drop-bottom-radius);
    animation-timing-function: cubic-bezier(0.2, 0.82, 0.26, 1);
  }

  84% {
    translate: 0 -28px;
    scale: 0.985 1.025;
    border-bottom-left-radius: var(--monster-drop-bottom-radius);
    border-bottom-right-radius: var(--monster-drop-bottom-radius);
    animation-timing-function: cubic-bezier(0.44, 0, 0.9, 0.62);
  }

  94% {
    translate: 0 0;
    scale: 1.02 0.975;
    border-bottom-left-radius: var(--monster-settle-bottom-radius);
    border-bottom-right-radius: var(--monster-settle-bottom-radius);
  }

  100% {
    opacity: 1;
    translate: 0 0;
    scale: 1 1;
    border-bottom-left-radius: var(--monster-rest-bottom-radius);
    border-bottom-right-radius: var(--monster-rest-bottom-radius);
  }
}
</style>
