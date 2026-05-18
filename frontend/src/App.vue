<template>
  <RouterView v-slot="{ Component }">
    <Transition name="page-fade" mode="out-in">
      <component :is="Component" />
    </Transition>
  </RouterView>
</template>

<script setup>
</script>

<style>
.page-fade-enter-active,
.page-fade-leave-active {
  transition:
    opacity 0.28s ease,
    transform 0.28s ease;
}

.page-fade-enter-from {
  opacity: 0;
  transform: translateY(12px) scale(0.99);
}

.page-fade-leave-to {
  opacity: 0;
  transform: translateY(-8px) scale(0.99);
}
</style>
