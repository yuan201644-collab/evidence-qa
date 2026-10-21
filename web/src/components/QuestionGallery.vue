<script setup lang="ts">
import { ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { QUESTION_GALLERY } from '../data/questionGallery'

/**
 * 问题灵感面板：点输入框左侧的四角星弹出。
 *
 * 为什么做成弹层而不是把首页示例卡加多：
 * 首页卡片是「第一眼引导」，两张正好；这里要承载的是完整能力面——
 * 按能力分组、每组带「演示时看什么」，还承担向导师/评委展示架构的职责。
 * 塞在首页会把冷启动界面撑爆。
 *
 * 点击行为 = 填入输入框（不直接发送），与首页示例卡一致：
 * 用户看完问句自己决定发不发。只有追问建议 chips 是「点了直接发」，
 * 三者行为差异是有意设计的。
 */
const emit = defineEmits<{ pick: [question: string] }>()
const open = ref(false)
const activeId = ref(QUESTION_GALLERY[0]?.id ?? '')

function activeGroup() {
  return QUESTION_GALLERY.find((g) => g.id === activeId.value) ?? QUESTION_GALLERY[0]
}

function choose(group: string) {
  activeId.value = group
}

/** 填入后收起面板，让用户回到输入框上下文 */
function use(question: string) {
  emit('pick', question)
  open.value = false
}
</script>

<template>
  <div class="gallery">
    <button
      type="button"
      class="gallery__trigger"
      aria-label="问题灵感：看看能问什么"
      title="问题灵感：按能力分组的问题清单，点一句填入输入框"
      :aria-expanded="open"
      @click="open = !open"
    >
      <AppIcon name="spark" :size="19" />
    </button>
    <Transition name="vm-detail">
      <div v-if="open" class="gallery__pop" role="dialog" aria-label="问题灵感">
        <header class="gallery__head">
          <div>
            <b>能问什么？</b>
            <span>按系统能力分组；点一句填入输入框，可改后再发送。</span>
          </div>
          <button type="button" class="gallery__close" aria-label="关闭问题灵感" @click="open = false"><AppIcon name="close" :size="15" /></button>
        </header>
        <div class="gallery__body">
          <nav class="gallery__tabs" aria-label="能力分组">
            <button
              v-for="group in QUESTION_GALLERY"
              :key="group.id"
              type="button"
              :class="{ active: group.id === activeId }"
              :aria-selected="group.id === activeId"
              role="tab"
              @click="choose(group.id)"
            >
              <AppIcon :name="group.icon" :size="14" />
              <span>{{ group.title }}</span>
            </button>
          </nav>
          <div class="gallery__detail">
            <p class="gallery__purpose"><AppIcon name="layers" :size="13" />{{ activeGroup().purpose }}</p>
            <ul class="gallery__list">
              <li v-for="q in activeGroup().questions" :key="q.question">
                <button type="button" class="gallery__q" @click="use(q.question)">
                  <span>{{ q.question }}</span>
                  <small v-if="q.note">{{ q.note }}</small>
                  <AppIcon class="gallery__q-arrow" name="arrow" :size="13" />
                </button>
              </li>
            </ul>
            <p class="gallery__watch"><b>演示时看这里：</b>{{ activeGroup().watch }}</p>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.gallery { position: relative; display: flex; align-items: center; }
/* 四角星按钮是「能问什么」的唯一常驻入口，做得比普通图标按钮显眼：
   蓝白对角渐变底 + 品牌色描边，让冷启动用户一眼看到这里能翻问题清单。 */
.gallery__trigger { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; padding: 0; border: 1px solid var(--border-brand); border-radius: 9px; color: var(--brand-primary); background: linear-gradient(135deg, var(--brand-soft) 0%, var(--bg-surface) 45%, var(--brand-soft-strong) 100%); cursor: pointer; transition: color var(--motion-fast) var(--motion-ease), border-color var(--motion-fast) var(--motion-ease), box-shadow var(--motion-fast) var(--motion-ease), transform var(--motion-fast) var(--motion-ease); }
.gallery__trigger:hover { color: var(--brand-primary); border-color: var(--brand-primary); background: linear-gradient(135deg, var(--brand-soft-strong) 0%, var(--brand-soft) 55%, var(--bg-surface) 100%); box-shadow: var(--shadow-focus-ring); transform: translateY(-1px); }
.gallery__trigger[aria-expanded='true'] { color: var(--text-on-brand); border-color: var(--brand-primary); background: linear-gradient(135deg, var(--brand-primary) 0%, var(--brand-primary-weak) 100%); box-shadow: none; transform: none; }
.gallery__pop { position: absolute; bottom: calc(100% + 10px); left: 0; z-index: 40; width: min(620px, calc(100vw - 48px)); border: 1px solid var(--border-subtle); border-radius: 14px; background: var(--bg-surface); box-shadow: var(--shadow-card); overflow: hidden; }
.gallery__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding: 13px 15px 11px; border-bottom: 1px solid var(--border-subtle); }
.gallery__head b { display: block; font-size: 15px; color: var(--text-heading); }
.gallery__head span { display: block; margin-top: 2px; font-size: 12.5px; color: var(--text-secondary); }
.gallery__close { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; padding: 0; border: 0; border-radius: 7px; color: var(--text-faint); background: transparent; cursor: pointer; }
.gallery__close:hover { color: var(--text-heading); background: var(--bg-hover); }
.gallery__body { display: grid; grid-template-columns: 138px minmax(0, 1fr); }
.gallery__tabs { display: flex; flex-direction: column; gap: 2px; padding: 9px; border-right: 1px solid var(--border-subtle); background: var(--bg-subtle); }
.gallery__tabs button { display: flex; align-items: center; gap: 7px; padding: 7px 9px; border: 0; border-radius: 8px; font-size: 13.5px; color: var(--text-secondary); background: transparent; cursor: pointer; text-align: left; }
.gallery__tabs button:hover { color: var(--text-heading); background: var(--bg-hover); }
.gallery__tabs button.active { color: var(--brand-primary); background: var(--brand-soft); font-weight: 600; }
.gallery__detail { padding: 12px 15px 13px; min-width: 0; }
.gallery__purpose { display: flex; align-items: center; gap: 6px; margin: 0 0 9px; font-size: 12.5px; color: var(--text-secondary); }
.gallery__purpose svg { color: var(--brand-primary); flex: none; }
.gallery__list { display: flex; flex-direction: column; gap: 6px; margin: 0; padding: 0; list-style: none; }
.gallery__q { position: relative; display: block; width: 100%; padding: 8px 30px 8px 11px; border: 1px solid var(--border-subtle); border-radius: 9px; text-align: left; background: var(--bg-surface); cursor: pointer; transition: border-color var(--motion-fast) var(--motion-ease), background var(--motion-fast) var(--motion-ease); }
.gallery__q:hover { border-color: var(--border-brand); background: var(--brand-soft); }
.gallery__q span { display: block; font-size: 14px; color: var(--text-heading); }
.gallery__q small { display: block; margin-top: 2px; font-size: 12px; color: var(--text-muted); }
.gallery__q-arrow { position: absolute; right: 10px; top: 50%; transform: translateY(-50%); color: var(--text-faint); }
.gallery__q:hover .gallery__q-arrow { color: var(--brand-primary); }
.gallery__watch { margin: 10px 0 0; padding: 8px 11px; border-radius: 9px; background: var(--bg-subtle); font-size: 12.5px; line-height: 1.55; color: var(--text-secondary); }
.gallery__watch b { color: var(--text-heading); }
@media (max-width: 767px) {
  .gallery__pop { position: fixed; left: 12px; right: 12px; bottom: 96px; width: auto; }
  .gallery__body { grid-template-columns: 1fr; }
  .gallery__tabs { flex-direction: row; flex-wrap: wrap; border-right: 0; border-bottom: 1px solid var(--border-subtle); }
}
</style>
