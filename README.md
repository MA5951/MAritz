![](/DocsMaterial/logo.png)

# 🚀 **MAritz – FRC WPILOG Replay Over NT**

**MAritz** is a desktop application for replaying `.wpilog` files over NetworkTables in simulation.  
It includes a timeline UI, tray controls, and a minimalist, easy-to-use interface.

![Demo](/DocsMaterial/Demo.gif)

---

## ✨ **Features**
- 📂 **Open & replay `.wpilog` files** over NetworkTables.  
- 🖱️ **Tray icon control** for quick start/stop of log replay.  
- 📊 **AdvantageScope-style timeline** for easy navigation.  
- 🖥️ **Two UI modes**:
  - **Tray Window** – Compact controls and timeline.  
  - **Full Window** – Larger timeline view with detailed playback controls.  

---

## 📥 **Installation**
> ⚠️ **Note:** Python **must** be installed before using MAritz.

1. **Download & extract** the provided ZIP file to any folder.  
2. Open the `MAritz` folder.  
3. Run **`MAritz.bat`** to start the program.  

---

## ▶️ **Usage**
1. **Open a log file**  
2. **Start NT broadcast**  
3. **Start replaying!** 

💡 *Replay continues even after closing the main window or tray icon, until the process is stopped.*  
The log values are published every **20 ms**.  

To exit, **right-click** on the tray icon and select **Exit**.  

---

### 🖼️ **Tray Icon Status**
The tray icon changes according to the status..
- **Black & White** – No log loaded  
- **Red** – Log loaded, replay stopped 
- **Green** – Log loaded and replaying 

---

## 🤖 **Robot Code Usage**

MAritz publishes log values over NetworkTables, letting you retrieve them in your robot code and use them just like live hardware inputs.

Example:
```java
NetworkTableInstance.getDefault()
    .getTable("/Replay")
    .getEntry("/NT:/MALog/Subsystems/Swerve/Modules/Front Left/Drive Position")
    .getDouble(0);
```

> Note:
All data is published under the /Replay table.
Change the entry path to match your original logging structure.